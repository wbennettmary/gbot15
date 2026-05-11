"""
AWS Management routes for AWS infrastructure, Lambda, and EC2 management.
"""
import os
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import json
import io
import zipfile
import time
import traceback
import logging
import threading
import random
import hashlib
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from flask import Blueprint, request, jsonify, session, render_template, copy_current_request_context
from functools import wraps
from database import db, UserAppPassword, AwsGeneratedPassword, AwsConfig, ProxyConfig, TwoCaptchaConfig, ServiceAccount
from services.google_service_account import GoogleServiceAccount

# ... (existing imports)



# Default Constants (fallback values if not configured)
DEFAULT_LAMBDA_ROLE_NAME = "gbot-app-password-lambda-role"
DEFAULT_PRODUCTION_LAMBDA_NAME = "gbot-chromium"
DEFAULT_S3_BUCKET_NAME = "gbot-app-passwords"
DEFAULT_ECR_REPO_NAME = "gbot-app-password-worker"
DEFAULT_DYNAMODB_TABLE = "gbot-app-passwords"
DEFAULT_INSTANCE_NAME = "default"
ECR_IMAGE_TAG = "latest"
EC2_INSTANCE_NAME = "gbot-ec2-build-box"
EC2_ROLE_NAME = "gbot-ec2-build-role"
EC2_INSTANCE_PROFILE_NAME = "gbot-ec2-build-instance-profile"
EC2_SECURITY_GROUP_NAME = "gbot-ec2-build-sg"
EC2_KEY_PAIR_NAME = "gbot-ec2-build-key"

# Backwards compatibility aliases (will be overridden by get_naming_config)
LAMBDA_ROLE_NAME = DEFAULT_LAMBDA_ROLE_NAME
PRODUCTION_LAMBDA_NAME = DEFAULT_PRODUCTION_LAMBDA_NAME
S3_BUCKET_NAME = DEFAULT_S3_BUCKET_NAME
ECR_REPO_NAME = DEFAULT_ECR_REPO_NAME

logger = logging.getLogger(__name__)


def get_current_username():
    """
    Get the current logged-in user's username from session.
    Handles both new sessions (with 'user' key) and old sessions (with only 'user_id').
    Returns None if no user is logged in.
    """
    # First, try the direct session username
    username = session.get('user')
    if username:
        return username.split('@')[0].lower()
    
    # Fallback: Try to get username from user_id via database query
    user_id = session.get('user_id')
    if user_id:
        try:
            from database import User
            user = User.query.get(user_id)
            if user and user.username:
                return user.username.split('@')[0].lower()
        except Exception as e:
            logger.warning(f"Failed to recover username from user_id {user_id}: {e}")
    
    return None


def get_naming_config():
    """
    Get customizable naming configuration from database.
    Returns dict with all naming values for multi-tenant support.
    Each user/tenant can have their own instance with unique names.
    """
    # [MULTI-USER] Capture session user data immediately from request context
    # This must be done before pushing any new app context
    current_user = session.get('user')
    clean_user = None
    dynamic_lambda_prefix = None
    dynamic_dynamodb_table = None
    
    if current_user:
        # Enforce Lowercase Username (e.g. Angel -> angel)
        clean_user = current_user.split('@')[0].lower()
        dynamic_lambda_prefix = f"{clean_user}-chromium"
        dynamic_dynamodb_table = f"{clean_user}-app-passwords"
        
    print(f"[DEBUG] get_naming_config START: user={current_user} -> clean={clean_user}, dynamic_prefix={dynamic_lambda_prefix}, dynamic_dynamodb={dynamic_dynamodb_table}, session keys={list(session.keys()) if session else 'None'}", flush=True)

    try:
        from app import app
        with app.app_context():
            # [MULTI-ACCOUNT] Use helper to get active config for current user
            config = get_current_active_config()
            if config:
                instance_name = config.instance_name or DEFAULT_INSTANCE_NAME
                
                # [MULTI-USER] Scope Lambda Name to Logged-in User
                # If user is logged in, ALWAYS use dynamic name.
                if dynamic_lambda_prefix:
                    lambda_prefix = dynamic_lambda_prefix
                else:
                    lambda_prefix = config.lambda_prefix or DEFAULT_PRODUCTION_LAMBDA_NAME

                # ECR repo name: use DB value if set, otherwise construct from instance_name
                # This ensures ECR repo matches the actual created repo (e.g., 'dev1-app-password-worker')
                ecr_repo = config.ecr_repo_name if config.ecr_repo_name else f"{instance_name}-app-password-worker"
                s3_bucket = config.s3_bucket or f"{instance_name}-app-passwords"
                
                # [MULTI-USER] Scope DynamoDB table to Logged-in User (same as Lambda)
                if dynamic_dynamodb_table:
                    dynamodb_table = dynamic_dynamodb_table
                else:
                    dynamodb_table = config.dynamodb_table or f"{instance_name}-app-passwords"
                
                print(f"[DEBUG] get_naming_config DB SUCCESS: lambda_prefix={lambda_prefix}, dynamodb_table={dynamodb_table}", flush=True)
                return {
                    'instance_name': instance_name,
                    'lambda_prefix': lambda_prefix,
                    'lambda_role_name': f"{lambda_prefix}-lambda-role",
                    'production_lambda_name': lambda_prefix,
                    'ecr_repo_name': ecr_repo,
                    's3_bucket': s3_bucket,
                    'dynamodb_table': dynamodb_table,
                    'ec2_instance_name': f"{instance_name}-ec2-build-box",
                    'ec2_role_name': f"{instance_name}-ec2-build-role",
                    'ec2_instance_profile_name': f"{instance_name}-ec2-build-instance-profile",
                    'ec2_security_group_name': f"{instance_name}-ec2-build-sg",
                    'ec2_key_pair_name': f"{instance_name}-ec2-build-key",
                }
    except Exception as e:
        logger.warning(f"[CONFIG] Could not load naming config from database: {e}")
        import traceback
        traceback.print_exc()
    
    # Return defaults if config not found
    if dynamic_lambda_prefix:
        lambda_prefix = dynamic_lambda_prefix
    else:
        lambda_prefix = DEFAULT_PRODUCTION_LAMBDA_NAME
    
    # [MULTI-USER] DynamoDB table also uses user-based naming
    if dynamic_dynamodb_table:
        dynamodb_table = dynamic_dynamodb_table
    else:
        dynamodb_table = f"{DEFAULT_INSTANCE_NAME}-app-passwords"

    # Use DEFAULT_INSTANCE_NAME prefix for all fallback resource names
    instance_name = DEFAULT_INSTANCE_NAME
    
    print(f"[DEBUG] get_naming_config FALLBACK RETURN: lambda_prefix={lambda_prefix}, dynamodb_table={dynamodb_table}", flush=True)
    return {
        'instance_name': instance_name,
        'lambda_prefix': lambda_prefix,
        'lambda_role_name': f"{lambda_prefix}-lambda-role",
        'production_lambda_name': lambda_prefix,
        'ecr_repo_name': f"{instance_name}-app-password-worker",
        's3_bucket': f"{instance_name}-app-passwords",
        'dynamodb_table': dynamodb_table,
        'ec2_instance_name': f"{instance_name}-ec2-build-box",
        'ec2_role_name': f"{instance_name}-ec2-build-role",
        'ec2_instance_profile_name': f"{instance_name}-ec2-build-instance-profile",
        'ec2_security_group_name': f"{instance_name}-ec2-build-sg",
        'ec2_key_pair_name': f"{instance_name}-ec2-build-key",
    }

aws_manager = Blueprint('aws_manager', __name__)

@aws_manager.route('/api/aws/get-naming-config', methods=['GET'])
def get_naming_config_api():
    """Return saved resource naming configuration from database"""
    try:
        config = get_current_active_config()
        current_username = get_current_username()
        
        if config:
            instance_name = config.instance_name or DEFAULT_INSTANCE_NAME
            
            # [MULTI-USER] Dynamic naming based on logged-in user
            if current_username:
                lambda_prefix = f"{current_username}-chromium"
                dynamodb_table = f"{current_username}-app-passwords"
            else:
                lambda_prefix = config.lambda_prefix or DEFAULT_PRODUCTION_LAMBDA_NAME
                dynamodb_table = config.dynamodb_table or f"{instance_name}-app-passwords"
            
            return jsonify({
                'success': True,
                'config': {
                    'id': config.id,
                    'name': config.name,
                    # Use DB values if set, otherwise construct from instance_name prefix
                    'ecr_repo_name': config.ecr_repo_name if config.ecr_repo_name else f"{instance_name}-app-password-worker",
                    's3_bucket': config.s3_bucket or f"{instance_name}-app-passwords",
                    'dynamodb_table': dynamodb_table,
                    'lambda_prefix': lambda_prefix,
                    'instance_name': instance_name
                }
            })
        else:
            # No config found - use default instance_name prefix pattern
            instance_name = DEFAULT_INSTANCE_NAME
            
            # [MULTI-USER] Dynamic naming based on logged-in user
            if current_username:
                lambda_prefix = f"{current_username}-chromium"
                dynamodb_table = f"{current_username}-app-passwords"
            else:
                lambda_prefix = DEFAULT_PRODUCTION_LAMBDA_NAME
                dynamodb_table = f"{instance_name}-app-passwords"
            
            return jsonify({
                'success': True,
                'config': {
                    'ecr_repo_name': f"{instance_name}-app-password-worker",
                    's3_bucket': f"{instance_name}-app-passwords",
                    'dynamodb_table': dynamodb_table,
                    'lambda_prefix': lambda_prefix,
                    'instance_name': instance_name
                }
            })
    except Exception as e:
        logger.warning(f"[CONFIG] Error getting naming config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/service-accounts', methods=['GET'])
def list_service_accounts():
    """List all configured service accounts"""
    try:
        accounts = ServiceAccount.query.all()
        return jsonify({
            'success': True,
            'accounts': [{
                'id': acc.id,
                'name': acc.name,
                'client_email': acc.client_email,
                'project_id': acc.project_id,
                'admin_email': acc.admin_email,
                'created_at': acc.created_at.isoformat() if acc.created_at else None
            } for acc in accounts]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/service-accounts/upload', methods=['POST'])
def upload_service_account():
    """Upload and verify a new service account JSON key"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file part'}), 400
        
        file = request.files['file']
        name = request.form.get('name', '').strip()
        admin_email = request.form.get('admin_email', '').strip()

        if not file or not name or not admin_email:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        if file.filename == '':
            return jsonify({'success': False, 'error': 'No selected file'}), 400

        # Read and parse JSON
        try:
            json_content = file.read().decode('utf-8')
            data = json.loads(json_content)
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid JSON file'}), 400

        # Validate JSON structure
        required_fields = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        if not all(field in data for field in required_fields):
             return jsonify({'success': False, 'error': 'Invalid Service Account JSON format'}), 400

        if data['type'] != 'service_account':
             return jsonify({'success': False, 'error': 'JSON is not a service account key'}), 400

        # Create record temporarily to verify
        new_account = ServiceAccount(
            name=name,
            admin_email=admin_email,
            project_id=data['project_id'],
            client_email=data['client_email'],
            private_key_id=data['private_key_id'],
            json_content=json_content
        )
        
        db.session.add(new_account)
        db.session.flush() # Get ID without committing

        # Verify connection
        service = GoogleServiceAccount(new_account.id)
        success, message = service.verify_connection()

        if success:
            db.session.commit()
            return jsonify({'success': True, 'message': 'Service Account added and verified successfully'})
        else:
            db.session.rollback()
            return jsonify({'success': False, 'error': f'Verification failed: {message}'}), 400

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/service-accounts/<int:account_id>', methods=['DELETE'])
def delete_service_account(account_id):
    """Delete a service account"""
    try:
        account = ServiceAccount.query.get(account_id)
        if not account:
            return jsonify({'success': False, 'error': 'Account not found'}), 404
        
        db.session.delete(account)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Service Account deleted'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# Global executor for background tasks
executor = ThreadPoolExecutor(max_workers=20)
active_jobs = {}
jobs_lock = threading.Lock()  # Lock for job storage

# Track Lambda creation jobs
lambda_creation_jobs = {}
lambda_creation_lock = threading.Lock()

# --- File-Based Job Store (Fix for Gunicorn Multi-Worker) ---
JOBS_FILE = 'jobs.json'
jobs_file_lock = threading.Lock()

def load_jobs():
    """Load jobs from JSON file (shared across workers)"""
    try:
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading jobs file: {e}")
    return {}

def save_jobs(jobs_data):
    """Save jobs to JSON file (shared across workers)"""
    try:
        with jobs_file_lock:
            # First load existing to merge (in case other workers updated other jobs)
            # But for simplicity and since we key by job_id, we might overwrite.
            # Ideally we should read-modify-write.
            current_jobs = {}
            if os.path.exists(JOBS_FILE):
                try:
                    with open(JOBS_FILE, 'r') as f:
                        current_jobs = json.load(f)
                except:
                    pass
            
            # Update with new data
            current_jobs.update(jobs_data)
            
            with open(JOBS_FILE, 'w') as f:
                json.dump(current_jobs, f)
    except Exception as e:
        logger.error(f"Error saving jobs file: {e}")

# Initialize active_jobs from file
active_jobs = load_jobs()


# Global set to track emails currently being processed (prevent duplicates within a job)
processing_emails = set()
processing_lock = threading.Lock()

# Rate limiting semaphore - AWS account limit is typically 1000+ concurrent executions
# Calculate dynamically based on number of functions being invoked
# For 34 geos × 10 functions max = 340 functions, but we'll use a high limit to allow all parallel
# Each function processes 10 users max, so total concurrent Lambda executions = number of functions
MAX_CONCURRENT_LAMBDA_INVOCATIONS = 500  # High limit to allow all functions to start in parallel
lambda_invocation_semaphore = threading.Semaphore(MAX_CONCURRENT_LAMBDA_INVOCATIONS)

# Proxy rotation counter (thread-safe)
proxy_rotation_counter = 0
proxy_rotation_lock = threading.Lock()

def get_proxy_config():
    """Get proxy configuration from database"""
    try:
        from app import app
        with app.app_context():
            config = ProxyConfig.query.first()
            if config:
                return {
                    'enabled': config.enabled,
                    'proxies': config.proxies if config.proxies else ''
                }
        return None
    except Exception as e:
        logger.warning(f"[PROXY] Error getting proxy config: {e}")
        return None

def get_twocaptcha_config():
    """Get 2Captcha configuration from database"""
    try:
        from app import app
        from database import TwoCaptchaConfig
        with app.app_context():
            config = TwoCaptchaConfig.query.first()
            if config:
                result = {
                    'enabled': bool(config.enabled),  # Ensure it's a boolean
                    'api_key': config.api_key if config.api_key else ''
                }
                logger.info(f"[2CAPTCHA] Database config retrieved: enabled={result['enabled']}, api_key_length={len(result['api_key'])}")
                return result
            else:
                logger.warning("[2CAPTCHA] No 2Captcha configuration found in database")
                return None
    except Exception as e:
        logger.error(f"[2CAPTCHA] Error getting 2Captcha config from database: {e}")
        logger.error(traceback.format_exc())
        return None

def parse_proxy_list(proxy_text):
    """Parse proxy list from text (one per line: IP:PORT:USERNAME:PASSWORD)"""
    if not proxy_text:
        return []
    
    proxies = []
    for line in proxy_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        
        parts = line.split(':')
        if len(parts) == 4:
            proxies.append({
                'ip': parts[0],
                'port': parts[1],
                'username': parts[2],
                'password': parts[3],
                'full': line
            })
    
    return proxies

def get_rotated_proxy(proxy_list):
    """Get next proxy from list using round-robin rotation (thread-safe)"""
    if not proxy_list:
        return None
    
    global proxy_rotation_counter
    with proxy_rotation_lock:
        proxy = proxy_list[proxy_rotation_counter % len(proxy_list)]
        proxy_rotation_counter += 1
        return proxy

# Login required decorator
def login_required(f):
    """Decorator to require login"""
    from flask import redirect, url_for
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def get_boto3_session(access_key, secret_key, region):
    """Create boto3 session from credentials"""
    return boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region
    )

def get_account_id(session):
    """Get AWS account ID"""
    sts = session.client("sts")
    ident = sts.get_caller_identity()
    return ident["Account"]

@aws_manager.route('/aws')
@login_required
def aws_management():
    """AWS Management page - requires aws_management permission"""
    from database import User
    
    # Emergency access gets all permissions
    if session.get('emergency_access'):
        pass  # Allow access
    else:
        # Check role-based permissions
        user_id = session.get('user_id')
        if user_id:
            user = User.query.get(user_id)
            if user:
                # Define permissions for aws_management
                allowed_roles = ['admin', 'mailer']
                if user.role not in allowed_roles:
                    flash("Access denied: insufficient permissions", "danger")
                    return redirect('/dashboard')
    
    # Ensure table exists to prevent 500 errors if migration wasn't run
    try:
        inspector = db.inspect(db.engine)
        tables = inspector.get_table_names()
        if 'aws_generated_password' not in tables:
            db.create_all()
        if 'aws_config' not in tables:
            db.create_all()
    except Exception as e:
        logger.error(f"Auto-migration failed: {e}")
    
    return render_template('aws_management.html', user=session.get('user'), role=session.get('role'))

@aws_manager.route('/api/aws/test-connection', methods=['POST'])
@login_required
def test_connection():
    """Test AWS connection"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide Access Key, Secret Key and Region.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        account_id = get_account_id(session)
        
        # Use configurable ECR repo name for multi-tenant support
        naming_config = get_naming_config()
        ecr_repo_name = naming_config['ecr_repo_name']
        ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ecr_repo_name}:{ECR_IMAGE_TAG}"

        return jsonify({
            'success': True,
            'account_id': account_id,
            'region': region,
            'ecr_uri': ecr_uri,
            'instance_name': naming_config['instance_name']
        })
    except Exception as e:
        logger.error(f"Error testing connection: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/save-config', methods=['POST'])
@login_required
def save_aws_config():
    """Save AWS credentials configuration"""
    try:
        # Allow admin, mailer, and support to save config (needed for running infrastructure)
        allowed_roles = ['admin', 'mailer', 'support']
        if str(session.get('role', '')).lower() not in allowed_roles:
            return jsonify({'success': False, 'error': 'Insufficient privileges'}), 403
        
        # Ensure table exists and has required columns
        try:
            inspector = db.inspect(db.engine)
            if 'aws_config' not in inspector.get_table_names():
                db.create_all()
            else:
                # Check if multi-tenant columns exist, add them if missing
                columns = [col['name'] for col in inspector.get_columns('aws_config')]
                missing_columns = []
                
                if 'instance_name' not in columns:
                    missing_columns.append(('instance_name', "VARCHAR(100) DEFAULT 'default'"))
                if 'ecr_repo_name' not in columns:
                    missing_columns.append(('ecr_repo_name', "VARCHAR(255) DEFAULT 'gbot-app-password-worker'"))
                if 'lambda_prefix' not in columns:
                    missing_columns.append(('lambda_prefix', "VARCHAR(100) DEFAULT 'gbot-chromium'"))
                if 'dynamodb_table' not in columns:
                    missing_columns.append(('dynamodb_table', "VARCHAR(255) DEFAULT 'gbot-app-passwords'"))
                
                if missing_columns:
                    logger.info(f"[AWS_CONFIG] Adding missing columns: {[c[0] for c in missing_columns]}")
                    for col_name, col_type in missing_columns:
                        try:
                            db.session.execute(text(f'ALTER TABLE aws_config ADD COLUMN {col_name} {col_type}'))
                            db.session.commit()
                            logger.info(f"[AWS_CONFIG] ✓ Added column {col_name}")
                        except Exception as col_err:
                            db.session.rollback()
                            logger.warning(f"[AWS_CONFIG] Could not add column {col_name}: {col_err}")
        except Exception as e:
            logger.warning(f"Could not check/create aws_config table: {e}")
        
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        access_key_id = data.get('access_key_id', '').strip()
        secret_access_key = data.get('secret_access_key', '').strip()
        region = data.get('region', 'us-east-1').strip()
        ecr_uri = data.get('ecr_uri', '').strip()
        s3_bucket = data.get('s3_bucket', 'gbot-app-passwords').strip()
        
        # Multi-tenant naming configuration (with safe defaults)
        instance_name = data.get('instance_name', 'default').strip() or 'default'
        ecr_repo_name = data.get('ecr_repo_name', '').strip()
        lambda_prefix = data.get('lambda_prefix', 'gbot-chromium').strip() or 'gbot-chromium'
        dynamodb_table = data.get('dynamodb_table', 'gbot-app-passwords').strip() or 'gbot-app-passwords'
        
        # CRITICAL: Extract ecr_repo_name from ecr_uri if not provided separately
        # This ensures the saved ECR URI's repo name is used for Lambda operations
        if not ecr_repo_name and ecr_uri:
            import re
            ecr_match = re.match(r'\d+\.dkr\.ecr\.[^.]+\.amazonaws\.com/([^:]+)', ecr_uri)
            if ecr_match:
                ecr_repo_name = ecr_match.group(1)
                logger.info(f"[AWS_CONFIG] Extracted ECR repo name from URI: {ecr_repo_name}")
        
        # Fallback to default if still empty
        if not ecr_repo_name:
            ecr_repo_name = 'gbot-app-password-worker'

        if not access_key_id or not secret_access_key or not region:
            return jsonify({'success': False, 'error': 'Please provide Access Key ID, Secret Access Key and Region.'}), 400
        
        # Validate instance_name (alphanumeric, dashes, underscores only) - only if provided
        import re
        if instance_name and instance_name != 'default' and not re.match(r'^[a-zA-Z0-9_-]+$', instance_name):
            return jsonify({'success': False, 'error': 'Instance name can only contain letters, numbers, dashes and underscores.'}), 400

        # Get config by ID (if updating) or create new
        config_id = data.get('id')
        config = None
        
        if config_id:
            config = AwsConfig.query.get(config_id)
            if not config:
                return jsonify({'success': False, 'error': 'Configuration not found'}), 404
            logger.info(f"[AWS_CONFIG] Updating existing configuration ID: {config_id}")
        else:
            # Check if we should create a NEW one or update the singleton (legacy behavior)
            # If "create_new" flag is true, force create.
            # Otherwise, check if *any* exist. If none, create. If some exist, error?
            # For backward compatibility: if NO id is provided, and tables exist, 
            # we used to just grab .first(). 
            # Better approach: If ID is null, assume CREATE NEW if explicit, or UPDATE DEFAULT if one exists?
            # Let's support an explicit 'create_new' flag or imply it if ID is missing but 'name' is provided?
            
            # Simple Logic: If user wants to edit, must provide ID. If no ID, assume NEW ENTRY.
             # EXCEPT for the very first one.
             
            first_config = AwsConfig.query.first()
            if not first_config:
                 config = AwsConfig()
                 db.session.add(config)
                 logger.info("[AWS_CONFIG] Creating first (default) AWS config")
            elif data.get('create_new', False):
                 config = AwsConfig()
                 db.session.add(config)
                 logger.info("[AWS_CONFIG] Creating additional AWS config")
            else:
                 # Default to updating the first one if not specified (Legacy Support)
                 config = first_config
                 logger.info("[AWS_CONFIG] Updating default AWS config (Legacy Mode)")



        # Update fields
        config.name = data.get('name', 'My AWS Account').strip() or 'My AWS Account'
        
        # Update config - core fields (always required)
        config.access_key_id = access_key_id
        config.secret_access_key = secret_access_key
        config.region = region
        config.ecr_uri = ecr_uri if ecr_uri and ecr_uri != '(connect first)' else None
        config.s3_bucket = s3_bucket
        config.is_configured = True
        
        # Multi-tenant naming (only set if columns exist, use safe defaults)
        try:
            # Check if columns exist before setting them
            inspector = db.inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('aws_config')]
            
            if 'instance_name' in columns:
                config.instance_name = instance_name
            if 'ecr_repo_name' in columns:
                config.ecr_repo_name = ecr_repo_name
            if 'lambda_prefix' in columns:
                config.lambda_prefix = lambda_prefix
            if 'dynamodb_table' in columns:
                config.dynamodb_table = dynamodb_table
        except Exception as col_err:
            logger.warning(f"[AWS_CONFIG] Could not set multi-tenant fields (columns may not exist): {col_err}")
            # Continue without multi-tenant fields - not critical for basic save
        
        # Commit the changes
        try:
            db.session.commit()
            logger.info("[AWS_CONFIG] ✓ AWS configuration saved successfully")
            logger.info(f"[AWS_CONFIG] Saved: access_key_id={access_key_id[:4]}***, region={region}, s3_bucket={s3_bucket}")
            
            return jsonify({'success': True, 'message': 'AWS configuration saved successfully'})
        except Exception as commit_err:
            db.session.rollback()
            logger.error(f"[AWS_CONFIG] DB Commit Error: {commit_err}")
            return jsonify({'success': False, 'error': f"Database error: {str(commit_err)}"}), 500

    except Exception as e:
        logger.error(f"[AWS_CONFIG] Save Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def get_current_active_config():
    """
    Helper to get the active AwsConfig for the current logged-in user.
    Falls back to the first available config if no user preference is set.
    """
    from database import User
    
    user_id = session.get('user_id')
    
    if user_id:
        user = User.query.get(user_id)
        if user and user.active_aws_config_id:
            config = AwsConfig.query.get(user.active_aws_config_id)
            if config:
                return config
    
    # Fallback: Return the first config found (default behavior)
    return AwsConfig.query.first()

@aws_manager.route('/api/aws/list-configs', methods=['GET'])
@login_required
def list_aws_configs():
    """List all available AWS configurations (summary only)"""
    try:
        configs = AwsConfig.query.all()
        
        # Get current user's active selection
        active_id = None
        from database import User
        user_id = session.get('user_id')
        if user_id:
            user = User.query.get(user_id)
            active_id = user.active_aws_config_id if user else None
        
        # If no active selection, default to first
        if not active_id and configs:
             active_id = configs[0].id

        return jsonify({
            'success': True,
            'configs': [{
                'id': c.id,
                'name': c.name,
                'region': c.region,
                'active': (c.id == active_id),
                'access_key_masked': f"{c.access_key_id[:4]}***" if c.access_key_id else ""
            } for c in configs]
        })
    except Exception as e:
        logger.error(f"[AWS_LIST] Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/set-active-config', methods=['POST'])
@login_required
def set_active_config():
    """Set the active AWS account for the current user"""
    try:
        data = request.get_json()
        config_id = data.get('config_id')
        
        if not config_id:
             return jsonify({'success': False, 'error': 'Missing config_id'}), 400
             
        from database import User
        user = User.query.get(session.get('user_id'))
        
        if not user:
             return jsonify({'success': False, 'error': 'User not found'}), 404
             
        # Verify config exists
        config = AwsConfig.query.get(config_id)
        if not config:
             return jsonify({'success': False, 'error': 'Configuration not found'}), 404
             
        user.active_aws_config_id = config.id
        db.session.commit()
        
        return jsonify({'success': True, 'message': f"Switched to account: {config.name}"})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/delete-config/<int:config_id>', methods=['DELETE'])
@login_required
def delete_aws_config(config_id):
    """Delete an AWS configuration"""
    try:
        # Check permissions
        allowed_roles = ['admin']
        if str(session.get('role', '')).lower() not in allowed_roles:
            return jsonify({'success': False, 'error': 'Only admins can delete configurations'}), 403

        config = AwsConfig.query.get(config_id)
        if not config:
            return jsonify({'success': False, 'error': 'Configuration not found'}), 404
            
        # Prevent deleting the last config? Maybe good practice but optional.
        count = AwsConfig.query.count()
        if count <= 1:
             return jsonify({'success': False, 'error': 'Cannot delete the last configuration.'}), 400

        db.session.delete(config)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Configuration deleted'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


        if 'no such column' in error_msg.lower():
            error_msg = 'Database schema is outdated. Please run: python3 migrate_db.py'
        elif 'UNIQUE constraint' in error_msg:
            error_msg = 'Configuration already exists. Updating existing entry...'
        elif 'permission denied' in error_msg.lower():
            error_msg = 'Database permission error. Check file permissions.'
        
        return jsonify({'success': False, 'error': error_msg}), 500

@aws_manager.route('/api/aws/get-config', methods=['GET'])
@login_required
def get_aws_config():
    """Get AWS credentials configuration"""
    try:
        # Allow admin, mailer, and support to get config (needed for UI to function)
        allowed_roles = ['admin', 'mailer', 'support']
        if str(session.get('role', '')).lower() not in allowed_roles:
            return jsonify({'success': False, 'error': 'Insufficient privileges'}), 403
        
        # Ensure table exists
        try:
            inspector = db.inspect(db.engine)
            if 'aws_config' not in inspector.get_table_names():
                db.create_all()
        except Exception as e:
            logger.warning(f"Could not check/create aws_config table: {e}")
        # Get the user's active AWS configuration (not just the first one)
        config = get_current_active_config()
        
        # Get dynamic naming configuration (handles user scoping)
        naming_config = get_naming_config()
        
        # Get session user for dynamic naming
        _session_user = session.get('user')
        
        # [MULTI-USER] Compute lambda_prefix and dynamodb_table based on logged-in user
        if _session_user:
            _clean_user = _session_user.split('@')[0].lower()
            _lambda_prefix = f"{_clean_user}-chromium"
            _dynamodb_table = f"{_clean_user}-app-passwords"
        else:
            _lambda_prefix = naming_config.get('lambda_prefix', 'gbot-chromium')
            _dynamodb_table = naming_config.get('dynamodb_table', 'gbot-app-passwords')

        if not config or not config.is_configured:
            # Return naming config even without AWS credentials configured
            return jsonify({
                'success': True,
                'config': {
                    'lambda_prefix': _lambda_prefix,
                    'dynamodb_table': _dynamodb_table,
                    'instance_name': naming_config.get('instance_name', 'gbot'),
                    's3_bucket': naming_config.get('s3_bucket', 'gbot-app-passwords'),
                    'ecr_repo_name': naming_config.get('ecr_repo_name', 'gbot-app-password-worker'),
                    'is_configured': False
                },
                'message': 'AWS credentials not configured'
            })
        
        return jsonify({
            'success': True,
            'config': {
                'access_key_id': config.access_key_id,
                'secret_access_key': config.secret_access_key,
                'region': config.region,
                'ecr_uri': config.ecr_uri or '',
                's3_bucket': config.s3_bucket,
                'instance_name': naming_config.get('instance_name'),
                'ecr_repo_name': getattr(config, 'ecr_repo_name', 'gbot-app-password-worker') or 'gbot-app-password-worker',
                'lambda_prefix': _lambda_prefix,
                'dynamodb_table': _dynamodb_table
            }
        })
    except Exception as e:
        logger.error(f"Error getting AWS config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/create-dynamodb', methods=['POST'])
@login_required
def create_dynamodb_table():
    """Create DynamoDB table for app password storage"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # DynamoDB is centralized in eu-west-1 (same as delete function)
        dynamodb_region = 'eu-west-1'
        session = get_boto3_session(access_key, secret_key, dynamodb_region)
        dynamodb = session.client('dynamodb')
        
        # [DEBUG] Check Account ID
        sts = session.client('sts')
        account_id = sts.get_caller_identity()['Account']
        logger.info(f"[DYNAMODB] Operating on Account ID: {account_id}, Region: {dynamodb_region}")
        
        # Get table name from request (preferred) or database (fallback)
        table_name = data.get('table_name', '').strip() or get_naming_config().get('dynamodb_table', 'gbot-app-passwords')
        logger.info(f"[DYNAMODB] Using table name from REQUEST: {table_name} in region {dynamodb_region}")
        
        try:
            # Check if table exists
            dynamodb.describe_table(TableName=table_name)
            return jsonify({'success': True, 'message': f'Table {table_name} already exists'})
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            
            # Create table
            logger.info(f"[DYNAMODB] Creating table {table_name}...")
            dynamodb.create_table(
                TableName=table_name,
                KeySchema=[
                    {'AttributeName': 'email', 'KeyType': 'HASH'}  # Partition key
                ],
                AttributeDefinitions=[
                    {'AttributeName': 'email', 'AttributeType': 'S'}
                ],
                BillingMode='PAY_PER_REQUEST'  # On-demand pricing (no provisioned capacity)
            )
            
            # Wait for table to be created
            waiter = dynamodb.get_waiter('table_exists')
            waiter.wait(TableName=table_name, WaiterConfig={'Delay': 2, 'MaxAttempts': 30})
            
            logger.info(f"[DYNAMODB] ✓ Table {table_name} created successfully")
            return jsonify({
                'success': True, 
                'message': f'Table {table_name} created successfully',
                'account_id': account_id
            })
            
    except Exception as e:
        logger.error(f"Error creating DynamoDB table: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/empty-dynamodb-table', methods=['POST'])
@login_required
def empty_dynamodb_table():
    """Empty all items from the user's DynamoDB table (based on logged-in username)"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()

        if not access_key or not secret_key:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # Get the logged-in username and build the table name
        username = get_current_username()
        if not username:
            return jsonify({'success': False, 'error': 'Could not determine logged-in user.'}), 400
        
        table_name = f"{username}-app-passwords"
        
        # DynamoDB is centralized in eu-west-1
        dynamodb_region = 'eu-west-1'
        boto_session = get_boto3_session(access_key, secret_key, dynamodb_region)
        dynamodb = boto_session.resource('dynamodb', region_name=dynamodb_region)
        
        logger.info(f"[DYNAMODB] Emptying table: {table_name} for user: {username}")
        
        # Get the table
        table = dynamodb.Table(table_name)
        
        # Verify table exists
        try:
            table.load()
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return jsonify({'success': False, 'error': f'Table {table_name} does not exist.'}), 404
            raise
        
        # Scan and delete all items
        deleted_count = 0
        scan_kwargs = {}
        
        while True:
            response = table.scan(**scan_kwargs)
            items = response.get('Items', [])
            
            if not items:
                break
            
            # Batch delete items (DynamoDB allows up to 25 items per batch)
            with table.batch_writer() as batch:
                for item in items:
                    # The primary key is 'email' based on create_dynamodb_table
                    if 'email' in item:
                        batch.delete_item(Key={'email': item['email']})
                        deleted_count += 1
            
            # Check if there are more items to scan
            if 'LastEvaluatedKey' not in response:
                break
            scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
        
        logger.info(f"[DYNAMODB] ✓ Emptied table {table_name}: {deleted_count} items deleted")
        
        return jsonify({
            'success': True,
            'message': f'Table {table_name} emptied successfully. {deleted_count} items deleted.',
            'table_name': table_name,
            'deleted_count': deleted_count
        })
        
    except Exception as e:
        logger.error(f"Error emptying DynamoDB table: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

def ensure_user_s3_permissions(session):
    """Ensure the IAM user or role (associated with the access key) has S3 permissions for bucket operations"""
    try:
        # Get the IAM user/role name from the access key
        sts = session.client("sts")
        caller_identity = sts.get_caller_identity()
        user_arn = caller_identity.get("Arn", "")
        
        iam = session.client("iam")
        s3_full_access_arn = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
        
        # Extract user/role name from ARN (format: arn:aws:iam::ACCOUNT:user/USERNAME or arn:aws:iam::ACCOUNT:role/ROLENAME)
        if ":user/" in user_arn:
            user_name = user_arn.split(":user/")[-1]
            
            # Check if user already has AmazonS3FullAccess policy
            try:
                attached_policies = iam.list_attached_user_policies(UserName=user_name)
                policy_arns = [p['PolicyArn'] for p in attached_policies.get('AttachedPolicies', [])]
                
                if s3_full_access_arn in policy_arns:
                    logger.info(f"[IAM] User {user_name} already has AmazonS3FullAccess policy attached")
                    return True
                
                # Attach AmazonS3FullAccess policy to the user
                logger.info(f"[IAM] Attaching AmazonS3FullAccess policy to user {user_name}...")
                iam.attach_user_policy(
                    UserName=user_name,
                    PolicyArn=s3_full_access_arn
                )
                logger.info(f"[IAM] ✓ Successfully attached AmazonS3FullAccess policy to user {user_name}")
                time.sleep(2)  # Wait for propagation
                return True
                
            except iam.exceptions.NoSuchEntityException:
                logger.warning(f"[IAM] User {user_name} not found. Cannot attach S3 permissions.")
                return False
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', '')
                if error_code == 'AccessDenied':
                    logger.warning(f"[IAM] Access denied when trying to attach S3 policy to user {user_name}. User may need admin permissions.")
                    return False
                raise e
                
        elif ":role/" in user_arn:
            # Handle IAM role
            role_name = user_arn.split(":role/")[-1].split("/")[-1]  # Handle role paths like role/path/name
            
            # Check if role already has AmazonS3FullAccess policy
            try:
                attached_policies = iam.list_attached_role_policies(RoleName=role_name)
                policy_arns = [p['PolicyArn'] for p in attached_policies.get('AttachedPolicies', [])]
                
                if s3_full_access_arn in policy_arns:
                    logger.info(f"[IAM] Role {role_name} already has AmazonS3FullAccess policy attached")
                    return True
                
                # Attach AmazonS3FullAccess policy to the role
                logger.info(f"[IAM] Attaching AmazonS3FullAccess policy to role {role_name}...")
                iam.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=s3_full_access_arn
                )
                logger.info(f"[IAM] ✓ Successfully attached AmazonS3FullAccess policy to role {role_name}")
                time.sleep(2)  # Wait for propagation
                return True
                
            except iam.exceptions.NoSuchEntityException:
                logger.warning(f"[IAM] Role {role_name} not found. Cannot attach S3 permissions.")
                return False
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', '')
                if error_code == 'AccessDenied':
                    logger.warning(f"[IAM] Access denied when trying to attach S3 policy to role {role_name}. Role may need admin permissions.")
                    return False
                raise e
        else:
            logger.warning(f"[IAM] Could not determine IAM user/role from ARN: {user_arn}")
            return False
            
    except Exception as e:
        logger.warning(f"[IAM] Could not ensure S3 permissions: {e}")
        logger.warning(f"[IAM] You may need to manually attach AmazonS3FullAccess policy to your IAM user/role")
        return False

@aws_manager.route('/api/aws/create-infrastructure', methods=['POST'])
@login_required
def create_infrastructure():
    """Create core AWS infrastructure (IAM, ECR, S3)"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        
        # Get custom naming from request
        ecr_repo_name = data.get('ecr_repo_name', 'gbot-app-password-worker').strip() or 'gbot-app-password-worker'
        s3_bucket = data.get('s3_bucket', 'gbot-app-passwords').strip() or 'gbot-app-passwords'
        iam_role_prefix = data.get('iam_role_prefix', 'gbot').strip() or 'gbot'
        
        # Extract common prefix from resource names (e.g., "dev" from "dev-ec2-build-box")
        # Use the first part before first dash, or use iam_role_prefix as fallback
        prefix = iam_role_prefix
        if '-' in ecr_repo_name:
            prefix = ecr_repo_name.split('-')[0]
        elif '-' in s3_bucket:
            prefix = s3_bucket.split('-')[0]
        
        logger.info(f"[INFRA] Using prefix: {prefix}")
        logger.info(f"[INFRA] ECR Repo: {ecr_repo_name}, S3 Bucket: {s3_bucket}, IAM Prefix: {iam_role_prefix}")

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide Access Key, Secret Key and Region.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        
        # Ensure user's IAM user has S3 permissions for bucket operations
        logger.info("[INFRA] Ensuring IAM user has S3 permissions...")
        ensure_user_s3_permissions(session)
        
        # Create IAM role for Lambda with custom name
        lambda_role_name = f"{prefix}-lambda-role"
        lambda_policies = [
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",  # For app password storage
        ]
        logger.info(f"[INFRA] Creating IAM role: {lambda_role_name}")
        role_arn = create_iam_role(session, lambda_role_name, "lambda.amazonaws.com", lambda_policies)

        # Create ECR repo with custom name
        logger.info(f"[INFRA] Creating ECR repo: {ecr_repo_name}")
        create_ecr_repo(session, region, ecr_repo_name)

        # Create S3 bucket with custom name
        logger.info(f"[INFRA] Creating S3 bucket: {s3_bucket}")
        actual_bucket_name = create_s3_bucket(session, region, s3_bucket)
        if actual_bucket_name and actual_bucket_name != s3_bucket:
            logger.info(f"[INFRA] Bucket created/verified with name: {actual_bucket_name} (requested: {s3_bucket})")
            s3_bucket = actual_bucket_name  # Use the actual bucket name

        return jsonify({
            'success': True,
            'role_arn': role_arn,
            'lambda_role_name': lambda_role_name,
            'ecr_repo_name': ecr_repo_name,
            's3_bucket': s3_bucket,
            'prefix': prefix,
            'message': f'Infrastructure setup completed with prefix "{prefix}". S3 permissions have been ensured for your IAM user.'
        })
    except Exception as e:
        logger.error(f"Error creating infrastructure: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/create-ecr-manual', methods=['POST'])
@login_required
def create_ecr_manual():
    """Manually create ECR repository"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide Access Key, Secret Key and Region.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        
        # Get ECR repo name from request (preferred) or database (fallback)
        ecr_repo_name = data.get('ecr_repo_name', '').strip() or get_naming_config().get('ecr_repo_name', 'gbot-app-password-worker')
        logger.info(f"[ECR] Creating ECR repo: {ecr_repo_name}")
        
        create_ecr_repo(session, region, ecr_repo_name)

        ecr = session.client("ecr")
        resp = ecr.describe_repositories(repositoryNames=[ecr_repo_name])
        repo_uri = resp['repositories'][0]['repositoryUri']

        return jsonify({
            'success': True,
            'repo_uri': repo_uri,
            'instance_name': naming_config['instance_name']
        })
    except Exception as e:
        logger.error(f"Error creating ECR repository: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/generate-ecr-push-script', methods=['POST'])
@login_required
def generate_ecr_push_script():
    """Generate a bash script to push ECR image to all regions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        base_ecr_uri = data.get('ecr_uri', '').strip()
        source_region_override = data.get('source_region', '').strip()
        
        if not access_key or not secret_key or not base_ecr_uri:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials and ECR URI.'}), 400
        
        if 'amazonaws.com' not in base_ecr_uri:
            return jsonify({'success': False, 'error': 'Invalid ECR URI format.'}), 400
        
        # Parse ECR URI
        import re
        ecr_match = re.match(r'(\d+)\.dkr\.ecr\.([^.]+)\.amazonaws\.com/([^:]+):(.+)', base_ecr_uri)
        if not ecr_match:
            return jsonify({'success': False, 'error': 'Could not parse ECR URI.'}), 400
        
        account_id, parsed_region, repo_name, image_tag = ecr_match.groups()
        source_region = source_region_override if source_region_override else parsed_region
        source_ecr_uri = f"{account_id}.dkr.ecr.{source_region}.amazonaws.com/{repo_name}:{image_tag}"
        
        # Get all available regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]
        
        target_regions = [r for r in AVAILABLE_GEO_REGIONS if r != source_region]
        
        # Generate bash script
        script_lines = [
            "#!/bin/bash",
            "# ECR Image Push Script - Push image to all AWS regions",
            f"# Source: {source_ecr_uri}",
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "set -e  # Exit on error",
            "",
            f"SOURCE_ECR_URI=\"{source_ecr_uri}\"",
            f"ACCOUNT_ID=\"{account_id}\"",
            f"REPO_NAME=\"{repo_name}\"",
            f"IMAGE_TAG=\"{image_tag}\"",
            f"SOURCE_REGION=\"{source_region}\"",
            "",
            "# Set AWS credentials",
            f"export AWS_ACCESS_KEY_ID=\"{access_key}\"",
            f"export AWS_SECRET_ACCESS_KEY=\"{secret_key}\"",
            "",
            "echo \"========================================\"",
            f"echo \"Pushing ECR image to {len(target_regions)} regions\"",
            f"echo \"Source: $SOURCE_ECR_URI\"",
            "echo \"========================================\"",
            "",
            "# Authenticate with source region",
            f"echo \"Authenticating with source region {source_region}...\"",
            f"aws ecr get-login-password --region $SOURCE_REGION | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$SOURCE_REGION.amazonaws.com",
            "",
            "# Pull image from source",
            "echo \"Pulling image from source region...\"",
            "docker pull $SOURCE_ECR_URI",
            "",
        ]
        
        # Add push commands for each target region
        for target_region in target_regions:
            target_ecr_uri = f"{account_id}.dkr.ecr.{target_region}.amazonaws.com/{repo_name}:{image_tag}"
            script_lines.extend([
                f"",
                f"echo \"\"",
                f"echo \"========================================\"",
                f"echo \"Processing {target_region}...\"",
                f"echo \"========================================\"",
                f"",
                f"# Ensure repository exists in {target_region}",
                f"echo \"Ensuring repository exists...\"",
                f"aws ecr create-repository --repository-name $REPO_NAME --region {target_region} || echo \"Repository likely exists, proceeding...\"",
                f"",
                f"# Authenticate with {target_region}",
                f"aws ecr get-login-password --region {target_region} | docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.{target_region}.amazonaws.com",
                f"",
                f"# Tag image for {target_region}",
                f"docker tag $SOURCE_ECR_URI {target_ecr_uri}",
                f"",
                f"# Push image to {target_region}",
                f"docker push {target_ecr_uri}",
                f"",
                f"echo \"✓ Successfully pushed to {target_region}\"",
            ])
        
        script_lines.extend([
            "",
            "echo \"\"",
            "echo \"========================================\"",
            "echo \"All images pushed successfully!\"",
            "echo \"========================================\"",
        ])
        
        script_content = "\n".join(script_lines)
        
        return jsonify({
            'success': True,
            'script': script_content,
            'filename': 'push-ecr-to-all-regions.sh',
            'instructions': [
                '1. Save the script to a file (e.g., push-ecr-to-all-regions.sh)',
                '2. Make it executable: chmod +x push-ecr-to-all-regions.sh',
                '3. Run it on a machine with Docker and AWS CLI installed',
                '4. Or run it on your EC2 build box via SSH'
            ]
        })
        
    except Exception as e:
        logger.error(f"Error generating ECR push script: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/push-ecr-to-all-regions', methods=['POST'])
@login_required
def push_ecr_to_all_regions():
    """Push ECR image to all available AWS regions for multi-region Lambda deployment"""
    try:
        data = request.get_json()
        # DEBUG: Log all keys received to find the correct repo name field
        safe_data = {k: v for k, v in data.items() if 'key' not in k and 'password' not in k}
        logger.info(f"[ECR] Received push request data: {safe_data}")
        
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        base_ecr_uri = data.get('ecr_uri', '').strip()
        source_region_override = data.get('source_region', '').strip()  # Allow manual selection
        # Check multiple possible keys for custom repo name to be robust
        custom_target_repo = data.get('repo_name', '').strip() or data.get('ecr_repo_name', '').strip() or data.get('repository_name', '').strip()
        
        if not access_key or not secret_key or not base_ecr_uri:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials and ECR URI.'}), 400
        
        if 'amazonaws.com' not in base_ecr_uri:
            return jsonify({'success': False, 'error': 'Invalid ECR URI format.'}), 400
        
        # Parse base ECR URI to extract components
        import re
        ecr_match = re.match(r'(\d+)\.dkr\.ecr\.([^.]+)\.amazonaws\.com/([^:]+):(.+)', base_ecr_uri)
        if not ecr_match:
            return jsonify({'success': False, 'error': 'Could not parse ECR URI. Format: account.dkr.ecr.region.amazonaws.com/repo:tag'}), 400
        
        account_id, parsed_region, repo_name, image_tag = ecr_match.groups()
        
        # Determine target repo name (use custom if provided, else use source name)
        target_repo_name = custom_target_repo if custom_target_repo else repo_name
        
        # Use override if provided, otherwise use parsed region from URI
        source_region = source_region_override if source_region_override else parsed_region
        
        # Construct source ECR URI using the selected source region
        source_ecr_uri = f"{account_id}.dkr.ecr.{source_region}.amazonaws.com/{repo_name}:{image_tag}"
        
        logger.info(f"[ECR] Source region: {source_region} (override: {source_region_override}, parsed: {parsed_region})")
        logger.info(f"[ECR] Source ECR URI: {source_ecr_uri}")
        logger.info(f"[ECR] Target Repo Name: {target_repo_name} (Source: {repo_name})")
        
        # Get all available AWS regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]
        
        # CRITICAL: Ensure ALL geos are included (except source region)
        target_regions = [r for r in AVAILABLE_GEO_REGIONS if r != source_region]
        logger.info(f"[ECR] Target regions: {len(target_regions)} (from {len(AVAILABLE_GEO_REGIONS)} total geos, excluding source {source_region})")
        
        # Verify no geos are missing
        if len(target_regions) != len(AVAILABLE_GEO_REGIONS) - 1:
            missing = set(AVAILABLE_GEO_REGIONS) - set(target_regions) - {source_region}
            if missing:
                logger.warning(f"[ECR] ⚠️ WARNING: Some geos missing from target list: {missing}")
                target_regions = [r for r in AVAILABLE_GEO_REGIONS if r != source_region]  # Rebuild to ensure completeness
        
        logger.info("=" * 60)
        logger.info(f"[ECR] Starting image replication to {len(target_regions)} regions")
        logger.info(f"[ECR] Source region: {source_region}")
        logger.info(f"[ECR] Source ECR URI: {source_ecr_uri}")
        logger.info(f"[ECR] Target regions: {', '.join(target_regions)}")
        logger.info("=" * 60)
        
        # Create job for tracking
        job_id = f"ecr_push_{int(time.time())}"
        with lambda_creation_lock:
            lambda_creation_jobs[job_id] = {
                'status': 'processing',
                'type': 'ecr_push',
                'total_regions': len(target_regions),
                'success_count': 0,
                'failure_count': 0,
                'results': {},
                'started_at': time.time()
            }
        
        def push_ecr_background():
            """Background task to push ECR image to all regions (PARALLEL)"""
            # Use a local copy of target_regions to avoid scope issues
            regions_to_process = list(target_regions)  # Create a copy from outer scope
            success_count = 0
            failure_count = 0
            results = {}
            results_lock = threading.Lock()
            
            try:
                # Check if Docker and AWS CLI are available
                docker_available = shutil.which('docker') is not None
                aws_cli_available = shutil.which('aws') is not None
                
                if not docker_available:
                    logger.warning("[ECR] ⚠️ Docker not found. Will only create repositories and provide push instructions.")
                if not aws_cli_available:
                    logger.warning("[ECR] ⚠️ AWS CLI not found. Cannot authenticate with ECR for Docker operations.")
                
                # Create source region session
                source_session = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=source_region
                )
                
                # ===== PRE-SCAN PHASE: Check which regions already have the image =====
                logger.info("=" * 60)
                logger.info("[ECR] PRE-SCAN: Checking which regions already have the ECR image...")
                logger.info("=" * 60)
                
                def check_region_image_status(check_region):
                    """Check if repository and image exist in a region"""
                    region_status = {
                        'region': check_region,
                        'repo_exists': False,
                        'image_exists': False,
                        'error': None
                    }
                    
                    try:
                        check_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=check_region
                        )
                        
                        # Verify credentials first
                        try:
                            sts = check_session.client('sts')
                            sts.get_caller_identity()
                        except Exception as cred_err:
                            region_status['error'] = f'Credential verification failed: {cred_err}'
                            return region_status
                        
                        ecr_check = check_session.client('ecr')
                        
                        # Check if repository exists
                        try:
                            ecr_check.describe_repositories(repositoryNames=[target_repo_name])
                            region_status['repo_exists'] = True
                        except ClientError as repo_err:
                            if repo_err.response['Error']['Code'] != 'RepositoryNotFoundException':
                                region_status['error'] = f'Repository check failed: {repo_err}'
                                return region_status
                            # Repository doesn't exist - that's OK, we'll create it
                        
                        # Check if image exists (only if repo exists)
                        if region_status['repo_exists']:
                            # Use list_images first to get all images, then check for our tag
                            # This is more reliable than describe_images with specific tag
                            try:
                                list_response = ecr_check.list_images(
                                    repositoryName=target_repo_name,
                                    maxResults=100  # Check up to 100 images
                                )
                                image_ids = list_response.get('imageIds', [])
                                
                                if image_ids:
                                    # Check if any image has our tag
                                    has_matching_tag = False
                                    found_tags = []
                                    for img in image_ids:
                                        img_tag = img.get('imageTag')
                                        if img_tag:
                                            found_tags.append(img_tag)
                                            if img_tag == image_tag:
                                                has_matching_tag = True
                                    
                                    if has_matching_tag:
                                        region_status['image_exists'] = True
                                        logger.info(f"[ECR] [{check_region}] ✓✓✓ Repository and image with tag '{image_tag}' EXIST (verified via list_images)")
                                    else:
                                        # Images exist but not with our tag
                                        # Double-check using describe_images to be absolutely sure
                                        try:
                                            ecr_check.describe_images(
                                                repositoryName=target_repo_name,
                                                imageIds=[{"imageTag": image_tag}],
                                            )
                                            # If describe_images succeeds, image exists
                                            region_status['image_exists'] = True
                                            logger.info(f"[ECR] [{check_region}] ✓✓✓ Repository and image with tag '{image_tag}' EXIST (verified via describe_images after list)")
                                        except ClientError as desc_check:
                                            if desc_check.response.get('Error', {}).get('Code') == 'ImageNotFoundException':
                                                # Confirmed: image with our tag doesn't exist
                                                logger.info(f"[ECR] [{check_region}] ⚠️ Repository exists with {len(image_ids)} image(s) [tags: {', '.join(found_tags[:5])}{'...' if len(found_tags) > 5 else ''}] but tag '{image_tag}' NOT found")
                                                # Need to push our specific tag
                                            else:
                                                # Other error - log but assume image doesn't exist
                                                logger.warning(f"[ECR] [{check_region}] ⚠️ Error in describe_images check: {desc_check}")
                                                # Need to push
                                else:
                                    # No images at all - double check with describe_images
                                    try:
                                        ecr_check.describe_images(
                                            repositoryName=target_repo_name,
                                            imageIds=[{"imageTag": image_tag}],
                                        )
                                        # If describe_images succeeds, image exists (list_images might have missed it)
                                        region_status['image_exists'] = True
                                        logger.info(f"[ECR] [{check_region}] ✓✓✓ Repository and image with tag '{image_tag}' EXIST (found via describe_images, list_images returned empty)")
                                    except ClientError as desc_check:
                                        if desc_check.response.get('Error', {}).get('Code') == 'ImageNotFoundException':
                                            # Confirmed: no images
                                            logger.info(f"[ECR] [{check_region}] ⚠️ Repository exists but NO images found (both list and describe confirm)")
                                            # Need to push
                                        else:
                                            logger.warning(f"[ECR] [{check_region}] ⚠️ Error in describe_images check: {desc_check}")
                                            # Need to push
                                    
                            except ClientError as list_err:
                                error_code = list_err.response.get('Error', {}).get('Code', '')
                                if error_code == 'RepositoryNotFoundException':
                                    # Repository doesn't actually exist (race condition?)
                                    region_status['repo_exists'] = False
                                    logger.warning(f"[ECR] [{check_region}] ⚠️ Repository check inconsistent - marking as not existing")
                                else:
                                    # Try fallback: describe_images with specific tag
                                    try:
                                        ecr_check.describe_images(
                                            repositoryName=target_repo_name,
                                            imageIds=[{"imageTag": image_tag}],
                                        )
                                        region_status['image_exists'] = True
                                        logger.info(f"[ECR] [{check_region}] ✓ Repository and image with tag '{image_tag}' exist (verified via describe_images fallback)")
                                    except ClientError as desc_err:
                                        desc_error_code = desc_err.response.get('Error', {}).get('Code', '')
                                        if desc_error_code == 'ImageNotFoundException':
                                            logger.info(f"[ECR] [{check_region}] ⚠️ Image with tag '{image_tag}' NOT found (both list and describe failed)")
                                            # Need to push
                                        else:
                                            logger.warning(f"[ECR] [{check_region}] ⚠️ Error checking image: {desc_error_code} - {desc_err}")
                                            region_status['error'] = f'Image check failed: {desc_error_code}'
                            except Exception as list_ex:
                                logger.error(f"[ECR] [{check_region}] ✗ Exception listing images: {list_ex}")
                                region_status['error'] = f'Exception listing images: {list_ex}'
                        else:
                            logger.info(f"[ECR] [{check_region}] ⚠️ Repository does NOT exist")
                            # Repository doesn't exist - need to create and push
                            
                    except Exception as check_err:
                        region_status['error'] = str(check_err)
                        logger.error(f"[ECR] [{check_region}] ✗ Error checking status: {check_err}")
                    
                    return region_status
                
                # Check all regions in parallel for faster pre-scan
                logger.info("=" * 60)
                logger.info(f"[ECR] PRE-SCAN PHASE: Checking {len(regions_to_process)} regions for existing images...")
                logger.info(f"[ECR] Looking for image tag: '{image_tag}' in repository: '{target_repo_name}'")
                logger.info("=" * 60)
                regions_to_push = []
                regions_with_image = []
                regions_with_repo_only = []
                regions_needing_repo = []
                
                with ThreadPoolExecutor(max_workers=20) as scan_executor:
                    scan_futures = {scan_executor.submit(check_region_image_status, region): region for region in regions_to_process}
                    
                    for scan_future in as_completed(scan_futures):
                        check_region = scan_futures[scan_future]
                        try:
                            status = scan_future.result()
                            
                            # Determine region status based on check results
                            if status.get('image_exists'):
                                # Image already exists - skip this region
                                regions_with_image.append(check_region)
                                logger.info(f"[ECR] [{check_region}] ✓✓✓ SKIP: Image with tag '{image_tag}' already exists")
                            elif status.get('error'):
                                # If there's an error, check if it's a critical error or just a warning
                                error_msg = status.get('error', '')
                                if 'Credential verification failed' in error_msg:
                                    # Critical: can't access region - skip it
                                    logger.error(f"[ECR] [{check_region}] ✗✗✗ SKIP: Credential error - cannot access region")
                                    # Don't add to push list - we can't push if credentials fail
                                else:
                                    # Other error - might be transient, try to push anyway
                                    logger.warning(f"[ECR] [{check_region}] ⚠️ Check error: {status['error']} - will attempt push anyway")
                                    regions_to_push.append(check_region)
                            elif status.get('repo_exists'):
                                # Repository exists but no image with our tag - need to push
                                regions_with_repo_only.append(check_region)
                                regions_to_push.append(check_region)
                                logger.info(f"[ECR] [{check_region}] ⚠️ NEEDS PUSH: Repository exists but image with tag '{image_tag}' missing")
                            else:
                                # No repository - need to create repo and push image
                                regions_needing_repo.append(check_region)
                                regions_to_push.append(check_region)
                                logger.info(f"[ECR] [{check_region}] ⚠️ NEEDS PUSH: Repository and image missing")
                                
                        except Exception as scan_err:
                            logger.error(f"[ECR] [{check_region}] ✗ Scan error: {scan_err} - will attempt push anyway")
                            regions_to_push.append(check_region)
                
                # Log summary with detailed breakdown
                logger.info("=" * 60)
                logger.info(f"[ECR] PRE-SCAN SUMMARY:")
                logger.info(f"[ECR]   Total regions scanned: {len(regions_to_process)}")
                logger.info(f"[ECR]   ✓ Regions with image (WILL SKIP): {len(regions_with_image)}")
                if regions_with_image:
                    logger.info(f"[ECR]      Skipped regions: {', '.join(sorted(regions_with_image))}")
                logger.info(f"[ECR]   ⚠️ Regions needing push (repo exists, image missing): {len(regions_with_repo_only)}")
                if regions_with_repo_only:
                    logger.info(f"[ECR]      Regions: {', '.join(sorted(regions_with_repo_only))}")
                logger.info(f"[ECR]   ⚠️ Regions needing push (repo + image missing): {len(regions_needing_repo)}")
                if regions_needing_repo:
                    logger.info(f"[ECR]      Regions: {', '.join(sorted(regions_needing_repo))}")
                logger.info(f"[ECR]   📊 TOTAL regions that need pushing: {len(regions_to_push)} out of {len(regions_to_process)}")
                if regions_to_push:
                    logger.info(f"[ECR]      Will push to: {', '.join(sorted(regions_to_push))}")
                logger.info("=" * 60)
                
                # Update job status with pre-scan results
                with lambda_creation_lock:
                    if job_id in lambda_creation_jobs:
                        lambda_creation_jobs[job_id]['pre_scan'] = {
                            'total_regions': len(regions_to_process),
                            'regions_with_image': len(regions_with_image),
                            'regions_to_push': len(regions_to_push),
                            'regions_with_image_list': sorted(regions_with_image),
                            'regions_to_push_list': sorted(regions_to_push)
                        }
                        lambda_creation_jobs[job_id]['message'] = f'Pre-scan complete: {len(regions_with_image)} already have image, pushing to {len(regions_to_push)} regions...'
                
                # If all regions already have the image, we're done!
                if not regions_to_push:
                    logger.info("[ECR] ✓✓✓ ALL regions already have the image! Nothing to push.")
                    with lambda_creation_lock:
                        if job_id in lambda_creation_jobs:
                            lambda_creation_jobs[job_id]['status'] = 'completed'
                            lambda_creation_jobs[job_id]['success_count'] = len(regions_with_image)
                            lambda_creation_jobs[job_id]['failure_count'] = 0
                            lambda_creation_jobs[job_id]['results'] = {
                                region: {'success': True, 'message': 'Image already exists (pre-scan verified)', 'aws_verified': True}
                                for region in regions_with_image
                            }
                    return
                
                # Update regions_to_process to only include regions that need pushing
                original_count = len(regions_to_process)
                regions_to_process = regions_to_push
                skipped_count = original_count - len(regions_to_process)
                
                logger.info("=" * 60)
                logger.info(f"[ECR] PRE-SCAN FILTERING COMPLETE:")
                logger.info(f"[ECR]   Original target regions: {original_count}")
                logger.info(f"[ECR]   Regions with image (SKIPPED): {skipped_count}")
                logger.info(f"[ECR]   Regions needing push: {len(regions_to_process)}")
                logger.info("=" * 60)
                
                if len(regions_to_process) == 0:
                    logger.info(f"[ECR] ✓✓✓ All {original_count} regions already have the image! No push needed.")
                else:
                    logger.info(f"[ECR] Proceeding to push to {len(regions_to_process)} region(s) that need the image...")
                    logger.info(f"[ECR] Target regions for push: {', '.join(sorted(regions_to_process))}")
                
                def push_to_region(target_region):
                    """Push ECR image to a single region (for parallel execution)"""
                    region_result = {'success': False, 'error': None}
                    try:
                        logger.info(f"[ECR] [{target_region}] Processing region...")
                        
                        # Create target region session
                        target_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=target_region
                        )
                        
                        # Verify credentials
                        try:
                            sts = target_session.client('sts')
                            sts.get_caller_identity()
                        except Exception as cred_err:
                            logger.error(f"[ECR] [{target_region}] ✗ Credential verification failed: {cred_err}")
                            region_result = {'success': False, 'error': f'Credential verification failed: {cred_err}'}
                            return region_result
                        
                        # Create ECR repository in target region if it doesn't exist
                        # (Pre-scan already identified this region needs pushing, but we verify repo status)
                        ecr_client = target_session.client('ecr')
                        repo_exists = False
                        try:
                            ecr_client.describe_repositories(repositoryNames=[target_repo_name])
                            repo_exists = True
                            logger.info(f"[ECR] [{target_region}] ✓ ECR repository already exists")
                        except ClientError as e:
                            if e.response['Error']['Code'] == 'RepositoryNotFoundException':
                                try:
                                    ecr_client.create_repository(
                                        repositoryName=target_repo_name,
                                        imageTagMutability='MUTABLE',
                                        imageScanningConfiguration={'scanOnPush': False}
                                    )
                                    logger.info(f"[ECR] [{target_region}] ✓ Created ECR repository")
                                    time.sleep(1)  # Reduced wait time
                                    repo_exists = True
                                except Exception as create_err:
                                    logger.error(f"[ECR] [{target_region}] ✗ Failed to create repository: {create_err}")
                                    region_result = {'success': False, 'error': f'Repository creation failed: {create_err}'}
                                    return region_result
                            else:
                                logger.error(f"[ECR] [{target_region}] ✗ Error checking repository: {e}")
                                region_result = {'success': False, 'error': f'Repository check failed: {e}'}
                                return region_result
                        
                        # Quick double-check: Verify image still doesn't exist (might have been pushed by another process)
                        # This is a fast check since pre-scan already determined it doesn't exist
                        target_ecr_uri = f"{account_id}.dkr.ecr.{target_region}.amazonaws.com/{target_repo_name}:{image_tag}"
                        if repo_exists:
                            try:
                                ecr_client.describe_images(
                                    repositoryName=target_repo_name,
                                    imageIds=[{"imageTag": image_tag}],
                                )
                                # Image exists now (might have been pushed by another process or EC2 script)
                                logger.info(f"[ECR] [{target_region}] ✓ Image now exists (was pushed by another process) - skipping")
                                region_result = {'success': True, 'message': 'Image already exists (verified during push)', 'aws_verified': True}
                                return region_result
                            except ClientError as e:
                                if e.response['Error']['Code'] != 'ImageNotFoundException':
                                    logger.error(f"[ECR] [{target_region}] ✗ Error checking image: {e}")
                                    region_result = {'success': False, 'error': f'Image check failed: {e}'}
                                    return region_result
                                # Image doesn't exist - proceed to push (as expected from pre-scan)
                        
                        # Image doesn't exist - attempt to push it using Docker
                        logger.info(f"[ECR] [{target_region}] Image not found. Attempting to push from {source_region}...")
                        
                        # First, verify source image exists
                        try:
                            source_session = boto3.Session(
                                aws_access_key_id=access_key,
                                aws_secret_access_key=secret_key,
                                region_name=source_region
                            )
                            source_ecr_client = source_session.client('ecr')
                            source_ecr_client.describe_images(
                                repositoryName=repo_name,
                                imageIds=[{"imageTag": image_tag}],
                            )
                            logger.info(f"[ECR] [{target_region}] ✓ Verified source image exists in {source_region}")
                        except ClientError as source_err:
                            if source_err.response['Error']['Code'] == 'ImageNotFoundException':
                                logger.error(f"[ECR] [{target_region}] ✗✗✗ CRITICAL: Source image does NOT exist in {source_region}! Cannot push.")
                                region_result = {'success': False, 'error': f'Source image not found in {source_region}. Please build and push the image to {source_region} first.'}
                                return region_result
                            else:
                                logger.warning(f"[ECR] [{target_region}] ⚠️ Could not verify source image: {source_err}")
                        
                        # Try to use EC2 build box if available (it has Docker)
                        # Prioritize EC2 build box over local Docker for reliability
                        ec2_instance_id = None
                        ec2_instance_state = None
                        using_ec2 = False
                        ec2_error_msg = None # Initialize error message tracking
                        try:
                            logger.info(f"[ECR] [{target_region}] Searching for EC2 build box...")
                            ec2_instance = find_ec2_build_instance(source_session)
                            if ec2_instance:
                                ec2_instance_id = ec2_instance['InstanceId']
                                ec2_instance_state = ec2_instance.get('State', {}).get('Name', 'unknown')
                                if ec2_instance_state == 'running':
                                    logger.info(f"[ECR] [{target_region}] ✓ Found running EC2 build box: {ec2_instance_id}, will use it for Docker operations")
                                    using_ec2 = True
                                else:
                                    ec2_error_msg = f"EC2 instance found but state is '{ec2_instance_state}' (not running)"
                                    logger.warning(f"[ECR] [{target_region}] ⚠️ {ec2_error_msg}")
                                    ec2_instance_id = None  # Don't use if not running
                            else:
                                ec2_error_msg = "No EC2 build box found matching name pattern"
                                logger.error(f"[ECR] [{target_region}] ✗ {ec2_error_msg}")
                        except Exception as ec2_err:
                            ec2_error_msg = f"Error finding EC2 build box: {ec2_err}"
                            logger.error(f"[ECR] [{target_region}] ✗ {ec2_error_msg}")
                            logger.error(traceback.format_exc())
                        
                        # Check if Docker is available locally (fallback option)
                        docker_available = shutil.which('docker') is not None
                        aws_cli_available = shutil.which('aws') is not None
                        
                        # PRIORITIZE EC2 build box if available and running (it's more reliable and has Docker pre-configured)
                        # Only use local Docker if EC2 is not available or not running
                        if ec2_instance_id and ec2_instance_state == 'running':
                            logger.info(f"[ECR] [{target_region}] Using EC2 build box {ec2_instance_id} for Docker operations")
                            # Use SSM to run Docker commands on EC2
                            try:
                                ssm_client = source_session.client('ssm')
                                
                                # Create a script to push image to target region
                                push_script = f"""#!/bin/bash
set -e
set -x  # Enable command tracing for debugging
export AWS_ACCESS_KEY_ID={access_key}
export AWS_SECRET_ACCESS_KEY={secret_key}
export AWS_DEFAULT_REGION={target_region}

# Authenticate with target region ECR
echo "=== Step 0: Ensuring repository {repo_name} exists in {target_region}... ==="
# Authenticate with target region ECR
echo "=== Step 0: Ensuring repository {repo_name} exists in {target_region}... ==="
aws ecr create-repository --repository-name {repo_name} --region {target_region} || echo "Repository creation returned non-zero (may already exist)."

echo "=== Step 1: Authenticating with ECR in {target_region}... ==="
if ! aws ecr get-login-password --region {target_region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{target_region}.amazonaws.com; then
    echo "ERROR: Failed to authenticate with target region ECR"
    exit 1
fi
echo "✓ Authenticated with target region ECR"

# Authenticate with source region ECR
echo "=== Step 2: Authenticating with ECR in {source_region}... ==="
if ! aws ecr get-login-password --region {source_region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{source_region}.amazonaws.com; then
    echo "ERROR: Failed to authenticate with source region ECR"
    exit 1
fi
echo "✓ Authenticated with source region ECR"

# Pull image from source region
echo "=== Step 3: Pulling image from {source_region}... ==="
if ! docker pull {source_ecr_uri}; then
    echo "ERROR: Failed to pull image from source region"
    exit 1
fi
echo "✓ Image pulled successfully"

# Tag image for target region
echo "=== Step 4: Tagging image for {target_region}... ==="
if ! docker tag {source_ecr_uri} {target_ecr_uri}; then
    echo "ERROR: Failed to tag image"
    exit 1
fi
echo "✓ Image tagged successfully"

# Push image to target region
echo "=== Step 5: Pushing image to {target_region}... ==="
if ! docker push {target_ecr_uri}; then
    echo "ERROR: Failed to push image to target region"
    exit 1
fi
echo "✓ Image pushed successfully"

# Verify image exists in target region (optimized for speed)
echo "=== Step 6: Verifying image exists in {target_region}... ==="
sleep 2  # Reduced wait time for ECR to update
for i in {{1..3}}; do
    if aws ecr describe-images --repository-name {target_repo_name} --image-ids imageTag={image_tag} --region {target_region} 2>&1; then
        echo "✓✓✓ VERIFIED: Image exists in ECR after push!"
        exit 0
    fi
    echo "Image not found yet (attempt $i/3), waiting..."
    sleep 2  # Reduced wait time
done

echo "ERROR: Image push completed but verification failed - image not found in ECR"
exit 1
"""
                                
                                # Run command via SSM
                                # CRITICAL: Increase timeout for large Docker images (can take 10-20+ minutes per region)
                                response = ssm_client.send_command(
                                    InstanceIds=[ec2_instance_id],
                                    DocumentName="AWS-RunShellScript",
                                    Parameters={
                                        'commands': [push_script],
                                        'workingDirectory': ['/home/ec2-user']
                                    },
                                    TimeoutSeconds=1800  # 30 minutes - optimized for faster processing
                                )
                                
                                command_id = response['Command']['CommandId']
                                logger.info(f"[ECR] [{target_region}] Started SSM command {command_id} on EC2 instance {ec2_instance_id}")
                                logger.info(f"[ECR] [{target_region}] Push script will: 1) Auth, 2) Pull from {source_region}, 3) Tag, 4) Push to {target_region}, 5) Verify")
                                
                                # Wait for command to complete (with extended timeout for large images)
                                max_wait = 1800  # 30 minutes - optimized for faster failure detection
                                wait_interval = 10  # Check every 10 seconds (faster status updates)
                                waited = 0
                                last_status = None
                                
                                while waited < max_wait:
                                    time.sleep(wait_interval)
                                    waited += wait_interval
                                    
                                    try:
                                        cmd_response = ssm_client.get_command_invocation(
                                            CommandId=command_id,
                                            InstanceId=ec2_instance_id
                                        )
                                        
                                        status = cmd_response['Status']
                                        
                                        # Log status changes
                                        if status != last_status:
                                            logger.info(f"[ECR] [{target_region}] SSM command status: {status} (waited {waited}s/{max_wait}s)")
                                            last_status = status
                                        
                                        # Log stdout every 2 minutes to see progress
                                        if waited % 120 == 0 and status == 'InProgress':
                                            stdout_content = cmd_response.get('StandardOutputContent', '')
                                            if stdout_content:
                                                # Show last few lines of output
                                                lines = stdout_content.strip().split('\n')
                                                last_lines = lines[-10:] if len(lines) > 10 else lines  # Show last 10 lines
                                                logger.info(f"[ECR] [{target_region}] Progress output (last 10 lines, waited {waited}s/{max_wait}s):")
                                                for line in last_lines:
                                                    if line.strip():  # Only log non-empty lines
                                                        logger.info(f"[ECR] [{target_region}]   {line}")
                                            else:
                                                logger.info(f"[ECR] [{target_region}] Still waiting for output... (status: {status}, waited {waited}s)")
                                        
                                        if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                                            if status == 'Success':
                                                logger.info(f"[ECR] [{target_region}] ✓✓✓ EC2 SSM command completed successfully")
                                            
                                            # CRITICAL: Verify the image actually exists after EC2 push
                                            logger.info(f"[ECR] [{target_region}] Verifying image exists after EC2 push...")
                                            time.sleep(2)  # Reduced wait time for ECR to update
                                            
                                            verification_attempts = 3  # Reduced from 5 to 3 for faster processing
                                            image_verified = False
                                            for verify_attempt in range(verification_attempts):
                                                try:
                                                    ecr_client.describe_images(
                                                        repositoryName=target_repo_name,
                                                        imageIds=[{"imageTag": image_tag}],
                                                    )
                                                    image_verified = True
                                                    logger.info(f"[ECR] [{target_region}] ✓✓✓ VERIFIED: Image exists in ECR after EC2 push!")
                                                    break
                                                except ClientError as verify_err:
                                                    if verify_err.response['Error']['Code'] == 'ImageNotFoundException':
                                                        logger.warning(f"[ECR] [{target_region}] Image not found yet (attempt {verify_attempt + 1}/{verification_attempts}), waiting...")
                                                        time.sleep(2)  # Reduced from 3 to 2 seconds
                                                    else:
                                                        logger.error(f"[ECR] [{target_region}] Verification error: {verify_err}")
                                                        break
                                            
                                            if not image_verified:
                                                logger.error(f"[ECR] [{target_region}] ✗✗✗ CRITICAL: EC2 push reported success but image NOT FOUND in ECR!")
                                                stdout_content = cmd_response.get('StandardOutputContent', '')
                                                logger.error(f"[ECR] [{target_region}] EC2 command stdout: {stdout_content[:1000]}")
                                                region_result = {'success': False, 'error': f'EC2 push completed but image verification failed - image not found in ECR after {verification_attempts} attempts'}
                                                return region_result
                                            
                                            logger.info(f"[ECR] [{target_region}] ✓✓✓ SUCCESS: Pushed and VERIFIED image via EC2 build box")
                                            region_result = {'success': True, 'message': f'Image pushed and verified successfully via EC2 build box'}
                                            return region_result
                                        else:
                                            error_details = cmd_response.get('StandardErrorContent', 'Unknown error')
                                            stdout_content = cmd_response.get('StandardOutputContent', '')
                                            logger.error(f"[ECR] [{target_region}] ✗ SSM command failed: {status}")
                                            logger.error(f"[ECR] [{target_region}] Error output: {error_details}")
                                            logger.error(f"[ECR] [{target_region}] Stdout output: {stdout_content[:1000]}")
                                            region_result = {'success': False, 'error': f'EC2 push failed: {status} - {error_details}'}
                                            return region_result
                                            break
                                    except Exception as status_err:
                                        logger.warning(f"[ECR] [{target_region}] Error checking SSM command status: {status_err}")
                                        # Continue waiting - might be transient error
                                        if waited % 120 == 0:  # Log every 2 minutes even on errors
                                            logger.info(f"[ECR] [{target_region}] Still waiting despite error... (waited {waited}s/{max_wait}s)")
                                
                                # If we exit the loop, it means we timed out
                                logger.error(f"[ECR] [{target_region}] ✗ EC2 push timed out after {max_wait}s ({max_wait/60:.1f} minutes)")
                                
                                # Try to get final status and output for debugging
                                try:
                                    final_response = ssm_client.get_command_invocation(
                                        CommandId=command_id,
                                        InstanceId=ec2_instance_id
                                    )
                                    final_status = final_response.get('Status', 'Unknown')
                                    stdout_content = final_response.get('StandardOutputContent', '')
                                    stderr_content = final_response.get('StandardErrorContent', '')
                                    
                                    logger.error(f"[ECR] [{target_region}] Final SSM status: {final_status}")
                                    if stdout_content:
                                        # Show last 1000 chars of output
                                        logger.error(f"[ECR] [{target_region}] Final stdout (last 1000 chars):\n{stdout_content[-1000:]}")
                                    if stderr_content:
                                        logger.error(f"[ECR] [{target_region}] Final stderr:\n{stderr_content}")
                                except Exception as final_err:
                                    logger.error(f"[ECR] [{target_region}] Could not get final SSM status: {final_err}")
                                
                                region_result = {'success': False, 'error': f'EC2 push timed out after {max_wait} seconds ({max_wait/60:.1f} minutes). Image may still be pushing - check EC2 instance logs via SSM or CloudWatch.'}
                                return region_result
                                
                            except Exception as ssm_err:
                                ec2_error_msg = str(ssm_err)
                                logger.error(f"[ECR] [{target_region}] ✗ Failed to use EC2 build box: {ssm_err}")
                                logger.error(traceback.format_exc())
                                # Fall through to local Docker or manual instructions
                        
                        # If we reach here, EC2 build box was not available, not running, or failed
                        # Try local Docker as fallback
                        if not docker_available:
                            logger.warning(f"[ECR] [{target_region}] ⚠️ Docker not available. Manual push required.")
                            
                            error_msg = "Docker not available. Manual push required."
                            if ec2_error_msg:
                                error_msg += f" (EC2 Attempt Failed: {ec2_error_msg})"
                            
                            region_result = {
                                'success': False,
                                'error': error_msg,
                                'instructions': [
                                    f'docker pull {source_ecr_uri}',
                                    f'docker tag {source_ecr_uri} {target_ecr_uri}',
                                    f'aws ecr get-login-password --region {target_region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{target_region}.amazonaws.com',
                                    f'docker push {target_ecr_uri}'
                                ]
                            }
                            return region_result
                        
                        if not aws_cli_available:
                            logger.warning(f"[ECR] [{target_region}] ⚠️ AWS CLI not available. Cannot authenticate with ECR.")
                            region_result = {
                                'success': False,
                                'error': 'AWS CLI not available. Cannot authenticate with ECR.',
                                'instructions': [
                                    f'Install AWS CLI and run:',
                                    f'aws ecr get-login-password --region {target_region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{target_region}.amazonaws.com',
                                    f'docker pull {source_ecr_uri}',
                                    f'docker tag {source_ecr_uri} {target_ecr_uri}',
                                    f'docker push {target_ecr_uri}'
                                ]
                            }
                            return region_result
                        
                        # Attempt to push using Docker
                        try:
                            # Step 0: Ensure repository exists (using Boto3)
                            try:
                                logger.info(f"[ECR] [{target_region}] Step 0: Ensuring repository exists...")
                                target_session = get_boto3_session(access_key, secret_key, target_region)
                                target_ecr = target_session.client("ecr")
                                try:
                                    target_ecr.create_repository(repositoryName=repo_name)
                                    logger.info(f"[ECR] [{target_region}] ✓ Created repository {repo_name}")
                                except ClientError as e:
                                    if e.response['Error']['Code'] == 'RepositoryAlreadyExistsException':
                                        logger.info(f"[ECR] [{target_region}] Repository {repo_name} already exists")
                                    else:
                                        logger.warning(f"[ECR] [{target_region}] Warning creating repo: {e}")
                            except Exception as repo_err:
                                logger.warning(f"[ECR] [{target_region}] Failed to ensure repository exists: {repo_err}")

                            logger.info(f"[ECR] [{target_region}] Step 1: Authenticating with ECR...")
                            # Authenticate with ECR
                            login_cmd = [
                                'aws', 'ecr', 'get-login-password',
                                '--region', target_region
                            ]
                            
                            # Set AWS credentials as environment variables for AWS CLI
                            env = os.environ.copy()
                            env['AWS_ACCESS_KEY_ID'] = access_key
                            env['AWS_SECRET_ACCESS_KEY'] = secret_key
                            env['AWS_DEFAULT_REGION'] = target_region
                            
                            login_process = subprocess.run(
                                login_cmd,
                                capture_output=True,
                                text=True,
                                env=env,
                                timeout=30
                            )
                            
                            if login_process.returncode != 0:
                                raise Exception(f"AWS CLI login failed: {login_process.stderr}")
                            
                            ecr_password = login_process.stdout.strip()
                            
                            # Docker login
                            docker_login_cmd = [
                                'docker', 'login',
                                '--username', 'AWS',
                                '--password-stdin',
                                f'{account_id}.dkr.ecr.{target_region}.amazonaws.com'
                            ]
                            
                            docker_login_process = subprocess.run(
                                docker_login_cmd,
                                input=ecr_password,
                                text=True,
                                capture_output=True,
                                timeout=30
                            )
                            
                            if docker_login_process.returncode != 0:
                                raise Exception(f"Docker login failed: {docker_login_process.stderr}")
                            
                            logger.info(f"[ECR] [{target_region}] ✓ Authenticated with ECR")
                            
                            # Pull image from source region
                            logger.info(f"[ECR] [{target_region}] Step 2: Pulling image from {source_region}...")
                            # Authenticate with source region first
                            source_login_cmd = [
                                'aws', 'ecr', 'get-login-password',
                                '--region', source_region
                            ]
                            source_login_process = subprocess.run(
                                source_login_cmd,
                                capture_output=True,
                                text=True,
                                env=env,
                                timeout=30
                            )
                            if source_login_process.returncode == 0:
                                source_password = source_login_process.stdout.strip()
                                source_docker_login = subprocess.run(
                                    ['docker', 'login', '--username', 'AWS', '--password-stdin', f'{account_id}.dkr.ecr.{source_region}.amazonaws.com'],
                                    input=source_password,
                                    text=True,
                                    capture_output=True,
                                    timeout=30
                                )
                            
                            pull_process = subprocess.run(
                                ['docker', 'pull', source_ecr_uri],
                                capture_output=True,
                                text=True,
                                timeout=600  # 10 minutes timeout for pull
                            )
                            
                            if pull_process.returncode != 0:
                                raise Exception(f"Docker pull failed: {pull_process.stderr}")
                            
                            logger.info(f"[ECR] [{target_region}] ✓ Pulled image from {source_region}")
                            
                            # Tag image for target region
                            logger.info(f"[ECR] [{target_region}] Step 3: Tagging image for {target_region}...")
                            tag_process = subprocess.run(
                                ['docker', 'tag', source_ecr_uri, target_ecr_uri],
                                capture_output=True,
                                text=True,
                                timeout=30
                            )
                            
                            if tag_process.returncode != 0:
                                raise Exception(f"Docker tag failed: {tag_process.stderr}")
                            
                            logger.info(f"[ECR] [{target_region}] ✓ Tagged image")
                            
                            # Push image to target region
                            logger.info(f"[ECR] [{target_region}] Step 4: Pushing image to {target_region}...")
                            push_process = subprocess.run(
                                ['docker', 'push', target_ecr_uri],
                                capture_output=True,
                                text=True,
                                timeout=1800  # 30 minutes timeout for push (increased for large images 1-3GB)
                            )
                            
                            if push_process.returncode != 0:
                                logger.error(f"[ECR] [{target_region}] Docker push stderr: {push_process.stderr}")
                                logger.error(f"[ECR] [{target_region}] Docker push stdout: {push_process.stdout}")
                                raise Exception(f"Docker push failed: {push_process.stderr}")
                            
                            logger.info(f"[ECR] [{target_region}] ✓✓✓ Docker push command completed")
                            
                            # CRITICAL: Verify the image actually exists after push (optimized for speed)
                            logger.info(f"[ECR] [{target_region}] Verifying image exists after push...")
                            time.sleep(2)  # Reduced wait time for ECR to update
                            
                            verification_attempts = 3  # Reduced from 5 to 3 for faster processing
                            image_verified = False
                            for verify_attempt in range(verification_attempts):
                                try:
                                    ecr_client.describe_images(
                                        repositoryName=repo_name,
                                        imageIds=[{"imageTag": image_tag}],
                                    )
                                    image_verified = True
                                    logger.info(f"[ECR] [{target_region}] ✓✓✓ VERIFIED: Image exists in ECR after push!")
                                    break
                                except ClientError as verify_err:
                                    if verify_err.response['Error']['Code'] == 'ImageNotFoundException':
                                        logger.warning(f"[ECR] [{target_region}] Image not found yet (attempt {verify_attempt + 1}/{verification_attempts}), waiting...")
                                        time.sleep(2)  # Reduced from 3 to 2 seconds for faster processing
                                    else:
                                        logger.error(f"[ECR] [{target_region}] Verification error: {verify_err}")
                                        break
                            
                            if not image_verified:
                                logger.error(f"[ECR] [{target_region}] ✗✗✗ CRITICAL: Image push reported success but image NOT FOUND in ECR!")
                                raise Exception(f"Image push completed but verification failed - image not found in ECR after {verification_attempts} attempts")
                            
                            logger.info(f"[ECR] [{target_region}] ✓✓✓ SUCCESS: Pushed and VERIFIED image in {target_region}")
                            region_result = {'success': True, 'message': f'Image pushed and verified successfully in {target_region}'}
                            return region_result
                            
                        except subprocess.TimeoutExpired:
                            logger.error(f"[ECR] [{target_region}] ✗ Timeout during Docker operation")
                            region_result = {'success': False, 'error': 'Timeout during Docker operation. Image may be large.'}
                            return region_result
                        except Exception as push_err:
                            logger.error(f"[ECR] [{target_region}] ✗ Failed to push image: {push_err}")
                            logger.error(traceback.format_exc())
                            region_result = {
                                'success': False,
                                'error': f'Docker push failed: {str(push_err)}',
                                'instructions': [
                                    f'docker pull {source_ecr_uri}',
                                    f'docker tag {source_ecr_uri} {target_ecr_uri}',
                                    f'aws ecr get-login-password --region {target_region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{target_region}.amazonaws.com',
                                    f'docker push {target_ecr_uri}'
                                ]
                            }
                            return region_result
                        
                    except Exception as region_err:
                        logger.error(f"[ECR] [{target_region}] ✗ Error processing region: {region_err}")
                        logger.error(traceback.format_exc())
                        region_result = {'success': False, 'error': str(region_err)}
                        return region_result
                
                # Check if we're using EC2 - if so, reduce parallelism to avoid overwhelming the instance
                # Docker images can be large (1-3GB), so pushing to multiple regions simultaneously can be slow
                using_ec2_for_any = False
                try:
                    ec2_check = find_ec2_build_instance(source_session)
                    if ec2_check and ec2_check.get('State', {}).get('Name') == 'running':
                        using_ec2_for_any = True
                except:
                    pass
                
                if using_ec2_for_any:
                    # When using EC2, increase parallelism for faster processing
                    # EC2 instances can handle more concurrent Docker operations
                    max_workers = min(len(regions_to_process), 8)  # Increased from 3 to 8 for faster processing
                    logger.info(f"[ECR] Using EC2 build box - processing {max_workers} regions in parallel for faster completion")
                    logger.info(f"[ECR] Each push may take 5-15 minutes for large images. Total time: ~{len(regions_to_process) * 10 / max_workers:.0f} minutes")
                else:
                    # When using local Docker or manual, can process more in parallel
                    max_workers = min(len(regions_to_process), 15)  # Increased from 10 to 15 for faster processing
                    logger.info(f"[ECR] Using local Docker/manual - processing up to {max_workers} regions in parallel")
                
                # Process all regions in PARALLEL using ThreadPoolExecutor
                logger.info(f"[ECR] Starting PARALLEL push to {len(regions_to_process)} regions...")
                logger.info(f"[ECR] Using {max_workers} parallel workers")
                
                # Update job status to processing
                with lambda_creation_lock:
                    if job_id in lambda_creation_jobs:
                        lambda_creation_jobs[job_id]['status'] = 'processing'
                        lambda_creation_jobs[job_id]['message'] = f'Pushing to {len(regions_to_process)} regions in parallel...'
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(push_to_region, region): region for region in regions_to_process}
                    
                    completed = 0
                    for future in as_completed(futures):
                        target_region = futures[future]
                        completed += 1
                        try:
                            region_result = future.result()
                            with results_lock:
                                results[target_region] = region_result
                                if region_result.get('success'):
                                    success_count += 1
                                    logger.info(f"[ECR] [{target_region}] ✓ Completed ({completed}/{len(regions_to_process)})")
                                else:
                                    failure_count += 1
                                    logger.error(f"[ECR] [{target_region}] ✗ Failed ({completed}/{len(regions_to_process)}): {region_result.get('error', 'Unknown error')}")
                            
                            # Update job status in real-time with AWS verification
                            with lambda_creation_lock:
                                if job_id in lambda_creation_jobs:
                                    # Verify success results against AWS in real-time
                                    if region_result.get('success'):
                                        try:
                                            verify_session = boto3.Session(
                                                aws_access_key_id=access_key,
                                                aws_secret_access_key=secret_key,
                                                region_name=target_region
                                            )
                                            verify_ecr = verify_session.client('ecr')
                                            verify_ecr.describe_images(
                                                repositoryName=repo_name,
                                                imageIds=[{"imageTag": image_tag}],
                                            )
                                            region_result['aws_verified'] = True
                                            logger.info(f"[ECR] [{target_region}] ✓ Real-time AWS verification: Image exists")
                                        except Exception as verify_err:
                                            logger.warning(f"[ECR] [{target_region}] ⚠️ Real-time verification failed: {verify_err}")
                                            region_result['aws_verified'] = False
                                    
                                    lambda_creation_jobs[job_id]['success_count'] = success_count
                                    lambda_creation_jobs[job_id]['failure_count'] = failure_count
                                    lambda_creation_jobs[job_id]['results'] = results.copy()
                                    lambda_creation_jobs[job_id]['message'] = f'Progress: {completed}/{len(regions_to_process)} regions completed ({success_count} success, {failure_count} failed)'
                                    lambda_creation_jobs[job_id]['last_update'] = time.time()
                                    
                        except Exception as e:
                            logger.error(f"[ECR] [{target_region}] Future error: {e}")
                            logger.error(traceback.format_exc())
                            with results_lock:
                                failure_count += 1
                                if target_region not in results:
                                    results[target_region] = {'success': False, 'error': f'Future execution error: {str(e)}'}
                            
                            # Update job status even on error
                            with lambda_creation_lock:
                                if job_id in lambda_creation_jobs:
                                    lambda_creation_jobs[job_id]['failure_count'] = failure_count
                                    lambda_creation_jobs[job_id]['results'] = results.copy()
                
                # Final AWS verification pass for all regions
                logger.info("[ECR] Performing final AWS verification pass...")
                verified_success = 0
                verified_failure = 0
                for target_region in regions_to_process:
                    try:
                        verify_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=target_region
                        )
                        verify_ecr = verify_session.client('ecr')
                        verify_ecr.describe_images(
                            repositoryName=repo_name,
                            imageIds=[{"imageTag": image_tag}],
                        )
                        # Image exists in AWS
                        if target_region in results:
                            results[target_region]['aws_verified'] = True
                        verified_success += 1
                        logger.info(f"[ECR] [{target_region}] ✓ Final verification: Image exists in AWS")
                    except ClientError as verify_err:
                        if verify_err.response['Error']['Code'] == 'ImageNotFoundException':
                            if target_region in results and results[target_region].get('success'):
                                logger.warning(f"[ECR] [{target_region}] ⚠️ Push reported success but image NOT in AWS!")
                                results[target_region]['aws_verified'] = False
                                results[target_region]['success'] = False
                                results[target_region]['error'] = 'Image not found in AWS after push'
                                verified_failure += 1
                        else:
                            logger.warning(f"[ECR] [{target_region}] ⚠️ Verification error: {verify_err}")
                
                # Update job status with verified results
                with lambda_creation_lock:
                    if job_id in lambda_creation_jobs:
                        lambda_creation_jobs[job_id]['status'] = 'completed'
                        lambda_creation_jobs[job_id]['success_count'] = verified_success
                        lambda_creation_jobs[job_id]['failure_count'] = verified_failure
                        lambda_creation_jobs[job_id]['results'] = results
                        lambda_creation_jobs[job_id]['completed_at'] = time.time()
                        lambda_creation_jobs[job_id]['message'] = f'Completed: {verified_success} verified success, {verified_failure} verified failed (AWS verified)'
                
                logger.info("=" * 60)
                logger.info(f"[ECR] Replication completed (AWS verified): {verified_success} success, {verified_failure} failed")
                logger.info("=" * 60)
                
                # CRITICAL: Verify ALL geos were processed
                processed_regions = set(results.keys())
                expected_regions = set(regions_to_process)
                missing_regions = expected_regions - processed_regions
                
                if missing_regions:
                    logger.error("=" * 60)
                    logger.error(f"[ECR] ✗✗✗ CRITICAL: {len(missing_regions)} region(s) were NOT processed!")
                    logger.error(f"[ECR] Missing regions: {sorted(missing_regions)}")
                    logger.error("=" * 60)
                    
                    # Retry missing regions
                    if missing_regions:
                        logger.info(f"[ECR] Retrying {len(missing_regions)} missing region(s)...")
                        retry_results = {}
                        with ThreadPoolExecutor(max_workers=min(len(missing_regions), 10)) as retry_executor:
                            retry_futures = {retry_executor.submit(push_to_region, region): region for region in missing_regions}
                            for future in as_completed(retry_futures):
                                retry_region = retry_futures[future]
                                try:
                                    retry_result = future.result()
                                    retry_results[retry_region] = retry_result
                                    if retry_result.get('success'):
                                        verified_success += 1
                                        logger.info(f"[ECR] [{retry_region}] ✓ Retry successful")
                                    else:
                                        verified_failure += 1
                                        logger.error(f"[ECR] [{retry_region}] ✗ Retry failed: {retry_result.get('error')}")
                                except Exception as retry_err:
                                    logger.error(f"[ECR] [{retry_region}] ✗ Retry exception: {retry_err}")
                                    verified_failure += 1
                                    retry_results[retry_region] = {'success': False, 'error': str(retry_err)}
                        
                        # Merge retry results
                        results.update(retry_results)
                        
                        # Update final status
                        with lambda_creation_lock:
                            if job_id in lambda_creation_jobs:
                                lambda_creation_jobs[job_id]['success_count'] = verified_success
                                lambda_creation_jobs[job_id]['failure_count'] = verified_failure
                                lambda_creation_jobs[job_id]['results'] = results
                                lambda_creation_jobs[job_id]['message'] = f'Completed with retries: {verified_success} verified success, {verified_failure} verified failed'
                
                # Final verification: Check that ALL expected regions are in results
                final_processed = set(results.keys())
                if final_processed != expected_regions:
                    logger.error(f"[ECR] ✗✗✗ FINAL CHECK FAILED: Still missing {len(expected_regions - final_processed)} region(s)")
                    logger.error(f"[ECR] Still missing: {sorted(expected_regions - final_processed)}")
                else:
                    logger.info(f"[ECR] ✓✓✓ FINAL CHECK PASSED: All {len(expected_regions)} expected regions were processed")
                
            except Exception as bg_err:
                logger.error(f"[ECR] Background task error: {bg_err}")
                logger.error(traceback.format_exc())
                with lambda_creation_lock:
                    if job_id in lambda_creation_jobs:
                        lambda_creation_jobs[job_id]['status'] = 'failed'
                        lambda_creation_jobs[job_id]['error'] = str(bg_err)
        
        # Start background thread
        threading.Thread(target=push_ecr_background, daemon=True).start()
        
        return jsonify({
            'success': True,
            'message': f'Started pushing ECR image from {source_region} to {len(target_regions)} regions. This may take several minutes.',
            'job_id': job_id,
            'source_region': source_region,
            'source_ecr_uri': source_ecr_uri,
            'target_regions': target_regions,
            'note': 'Check status using /api/aws/lambda-creation-status/<job_id>'
        })
        
    except Exception as e:
        logger.error(f"Error pushing ECR to regions: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/inspect-resources', methods=['POST'])
@login_required
def inspect_resources():
    """Inspect AWS resources"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide Access Key, Secret Key and Region.'}), 400

        session = get_boto3_session(access_key, secret_key, region)

        # Inspect IAM
        iam_roles = inspect_iam(session)
        ecr_repos = inspect_ecr(session)
        s3_buckets = inspect_s3(session)
        lambdas = inspect_lambdas(session)

        return jsonify({
            'success': True,
            'iam_roles': iam_roles,
            'ecr_repos': ecr_repos,
            's3_buckets': s3_buckets,
            'lambdas': lambdas
        })
    except Exception as e:
        logger.error(f"Error inspecting resources: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# Global list of available AWS regions
AVAILABLE_GEO_REGIONS = [
    # United States
    'us-east-1',      # N. Virginia
    'us-east-2',      # Ohio
    'us-west-1',      # N. California
    'us-west-2',      # Oregon
    # Africa
    'af-south-1',     # Cape Town
    # Asia Pacific
    'ap-east-1',      # Hong Kong
    'ap-east-2',      # Taipei
    'ap-south-1',     # Mumbai
    'ap-south-2',     # Hyderabad
    'ap-northeast-1', # Tokyo
    'ap-northeast-2', # Seoul
    'ap-northeast-3', # Osaka
    'ap-southeast-1', # Singapore
    'ap-southeast-2', # Sydney
    'ap-southeast-3', # Jakarta
    'ap-southeast-4', # Melbourne
    'ap-southeast-5', # Malaysia
    'ap-southeast-6', # New Zealand
    'ap-southeast-7', # Thailand
    # Canada
    'ca-central-1',   # Central
    'ca-west-1',      # Calgary
    # Europe
    'eu-central-1',   # Frankfurt
    'eu-west-1',      # Ireland
    'eu-west-2',      # London
    'eu-west-3',      # Paris
    'eu-north-1',     # Stockholm
    'eu-south-1',     # Milan
    'eu-south-2',     # Spain
    # Mexico
    'mx-central-1',   # Central
    # Middle East
    'me-south-1',     # Bahrain
    'me-central-1',   # UAE
    'il-central-1',   # Israel (Tel Aviv)
    # South America
    'sa-east-1',      # São Paulo
]

def inspect_region_task(access_key, secret_key, region, lambda_prefix=None, ecr_repo_name=None):
    """Inspect resources in a single region (for parallel execution)"""
    result = {
        'region': region,
        'status': 'unknown',
        'lambda_count': 0,
        'ecr_repo': False,
        'image_tag': None,
        'error': None,
        'lambda_prefix_searched': lambda_prefix or 'N/A',
        'ecr_repo_searched': ecr_repo_name or 'N/A',
        'lambda_functions_found': [],
        'ecr_repo_found': None
    }
    
    try:
        session = get_boto3_session(access_key, secret_key, region)
        
        # Get configurable naming from parameters (preferred) or database (fallback)
        if lambda_prefix is None or ecr_repo_name is None:
            naming_config = get_naming_config()
            if lambda_prefix is None:
                lambda_prefix = naming_config.get('production_lambda_name', 'gbot-chromium')
            if ecr_repo_name is None:
                ecr_repo_name = naming_config.get('ecr_repo_name', 'gbot-app-password-worker')
        
        logger.debug(f"[INSPECT] [{region}] Checking Lambda prefix: {lambda_prefix}, ECR repo: {ecr_repo_name}")
        
        # Check Lambda Functions
        lam = session.client('lambda', config=Config(connect_timeout=5, read_timeout=5, retries={'max_attempts': 2}))
        functions = lam.list_functions()
        func_names = [f['FunctionName'] for f in functions.get('Functions', [])]
        
        # Try exact prefix match first
        prod_funcs = [f for f in func_names if f.startswith(lambda_prefix)]
        
        # [DEBUG] Log filtering details
        if func_names:
            logger.debug(f"[INSPECT] [{region}] Filter Debug: Prefix='{lambda_prefix}'")
            logger.debug(f"[INSPECT] [{region}] All Functions: {func_names[:5]}... (Total {len(func_names)})")
            logger.debug(f"[INSPECT] [{region}] Matched Functions: {prod_funcs}")

        # [STRICT MODE] Do NOT fall back to searching for other patterns.
        # The user wants to see ONLY their resources.
        if not prod_funcs:
            logger.debug(f"[INSPECT] [{region}] No Lambdas found with prefix '{lambda_prefix}'")
        
        result['lambda_count'] = len(prod_funcs)
        result['lambda_functions_found'] = prod_funcs
        if prod_funcs:
            logger.debug(f"[INSPECT] [{region}] Found Lambda functions: {prod_funcs}")
        else:
            logger.debug(f"[INSPECT] [{region}] No Lambda functions found (searched with prefix '{lambda_prefix}', total functions in region: {len(func_names)})")
            if func_names:
                logger.debug(f"[INSPECT] [{region}] Available Lambda functions in region: {func_names[:10]}...")  # Show first 10
        
        # Check ECR Repo
        ecr = session.client('ecr', config=Config(connect_timeout=5, read_timeout=5, retries={'max_attempts': 2}))
        found_repo_name = None
        try:
            ecr.describe_repositories(repositoryNames=[ecr_repo_name])
            found_repo_name = ecr_repo_name
            result['ecr_repo'] = True
            result['ecr_repo_found'] = found_repo_name
            logger.debug(f"[INSPECT] [{region}] ECR repo '{ecr_repo_name}' exists")
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'RepositoryNotFoundException':
                # Try to find repos with similar names
                logger.debug(f"[INSPECT] [{region}] ECR repo '{ecr_repo_name}' not found, searching for similar repos...")
                try:
                    all_repos = ecr.describe_repositories()
                    repo_names = [r['repositoryName'] for r in all_repos.get('repositories', [])]
                    
                    # Try to find repos matching common patterns
                    # Extract base name (e.g., "app-password-worker" from "dev-app-password-worker")
                    base_patterns = ['app-password-worker', 'app-password', 'password-worker', 'worker']
                    for pattern in base_patterns:
                        matching_repos = [r for r in repo_names if pattern in r.lower()]
                        if matching_repos:
                            found_repo_name = matching_repos[0]  # Use first match
                            result['ecr_repo'] = True
                            result['ecr_repo_found'] = found_repo_name
                            logger.info(f"[INSPECT] [{region}] Found ECR repo '{found_repo_name}' (was looking for '{ecr_repo_name}')")
                            break
                    
                    if not found_repo_name:
                        result['ecr_repo'] = False
                        logger.debug(f"[INSPECT] [{region}] No matching ECR repos found. Available repos: {repo_names}")
                except Exception as search_err:
                    logger.warning(f"[INSPECT] [{region}] Error searching for ECR repos: {search_err}")
                    result['ecr_repo'] = False
            else:
                logger.warning(f"[INSPECT] [{region}] Error checking ECR repo: {e}")
                raise
        
        # Check Image if repo was found
        if result['ecr_repo'] and found_repo_name:
            try:
                images = ecr.list_images(repositoryName=found_repo_name, filter={'tagStatus': 'TAGGED'})
                tags = [i.get('imageTag') for i in images.get('imageIds', [])]
                logger.debug(f"[INSPECT] [{region}] Found image tags in '{found_repo_name}': {tags}")
                if ECR_IMAGE_TAG in tags:
                    result['image_tag'] = ECR_IMAGE_TAG
                    logger.debug(f"[INSPECT] [{region}] ✓ Image tag '{ECR_IMAGE_TAG}' found")
                else:
                    result['image_tag'] = 'MISSING'
                    logger.debug(f"[INSPECT] [{region}] ✗ Image tag '{ECR_IMAGE_TAG}' not found (available tags: {tags})")
            except Exception as img_err:
                logger.warning(f"[INSPECT] [{region}] Error checking images: {img_err}")
                result['image_tag'] = 'ERROR'
                
        result['status'] = 'ready' if result['lambda_count'] > 0 and result['ecr_repo'] and result['image_tag'] == ECR_IMAGE_TAG else 'incomplete'
        logger.info(f"[INSPECT] [{region}] Status: {result['status']} (Lambdas: {result['lambda_count']}, ECR: {result['ecr_repo']}, Image: {result['image_tag']})")
        
    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)
        logger.error(f"[INSPECT] [{region}] Error during inspection: {e}")
        import traceback
        logger.debug(f"[INSPECT] [{region}] Traceback: {traceback.format_exc()}")
        
    return result

@aws_manager.route('/api/aws/inspect-all-regions', methods=['POST'])
@login_required
def inspect_all_regions():
    """Inspect resources across ALL regions in parallel"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        
        # Get naming configuration from request (preferred) or database (fallback)
        lambda_prefix = data.get('lambda_prefix', '').strip() or get_naming_config().get('production_lambda_name', 'gbot-chromium')
        ecr_repo_name = data.get('ecr_repo_name', '').strip() or get_naming_config().get('ecr_repo_name', 'gbot-app-password-worker')
        
        logger.info(f"[INSPECT] Starting global inspection for {len(AVAILABLE_GEO_REGIONS)} regions...")
        logger.info(f"[INSPECT] Using Lambda prefix: {lambda_prefix}")
        logger.info(f"[INSPECT] Using ECR repo: {ecr_repo_name}")
        
        if not access_key or not secret_key:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400
        
        results = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            future_to_region = {
                pool.submit(inspect_region_task, access_key, secret_key, region, lambda_prefix, ecr_repo_name): region 
                for region in AVAILABLE_GEO_REGIONS
            }
            
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    data = future.result()
                    results.append(data)
                except Exception as exc:
                    logger.error(f"[INSPECT] Region {region} generated an exception: {exc}")
                    results.append({
                        'region': region,
                        'status': 'error',
                        'error': str(exc)
                    })
                    
        # Sort results by region name
        results.sort(key=lambda x: x['region'])
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        logger.error(f"Error in global inspection: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/create-lambdas', methods=['POST'])
@login_required
def create_lambdas():
    """Create/Update production Lambda(s) based on user count"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        ecr_uri = data.get('ecr_uri', '').strip()
        s3_bucket = data.get('s3_bucket', '').strip()
        user_count = data.get('user_count', 0)  # Number of users (auto-calculated from input field)
        users_per_function_raw = data.get('users_per_function', 10)  # Read from request, default to 10
        
        # DEBUG: Log the raw value received
        logger.info(f"[LAMBDA] ========== DEBUG: users_per_function from request ==========")
        logger.info(f"[LAMBDA] Raw value received: {users_per_function_raw} (type: {type(users_per_function_raw)})")
        logger.info(f"[LAMBDA] Full request data keys: {list(data.keys())}")
        logger.info(f"[LAMBDA] =============================================================")
        
        # Validate users_per_function
        try:
            users_per_function = int(users_per_function_raw)
            if users_per_function < 1 or users_per_function > 20:
                users_per_function = 10  # Reset to default if invalid
                logger.warning(f"[LAMBDA] ⚠️ Invalid users_per_function value ({users_per_function_raw}), using default: 10")
            else:
                logger.info(f"[LAMBDA] ✓ Valid users_per_function value: {users_per_function}")
        except (ValueError, TypeError) as e:
            users_per_function = 10
            logger.warning(f"[LAMBDA] ⚠️ Invalid users_per_function type ({type(users_per_function_raw)}): {e}, using default: 10")
        
        logger.info(f"[LAMBDA] Final users_per_function value: {users_per_function}")
        
        create_multiple = data.get('create_multiple', False)  # Whether to create multiple functions

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        if not ecr_uri or 'amazonaws.com' not in ecr_uri:
            return jsonify({'success': False, 'error': 'ECR Image URI is not set. Connect and prepare EC2 build box first.'}), 400

        if not s3_bucket:
            return jsonify({'success': False, 'error': 'Please enter S3 Bucket name for app passwords storage.'}), 400

        # Get customizable naming configuration from request (preferred) or database (fallback)
        lambda_prefix = data.get('lambda_prefix', '').strip() or get_naming_config().get('production_lambda_name', 'gbot-chromium')
        ecr_repo_name = data.get('ecr_repo_name', '').strip() or get_naming_config().get('ecr_repo_name', 'gbot-app-password-worker')
        dynamodb_table = data.get('dynamodb_table', '').strip() or get_naming_config().get('dynamodb_table', 'gbot-app-passwords')
        
        # Extract prefix from lambda_prefix (e.g., "dev" from "dev-chromium")
        prefix = lambda_prefix.split('-')[0] if '-' in lambda_prefix else lambda_prefix.split('_')[0] if '_' in lambda_prefix else 'gbot'
        instance_name = prefix  # Use prefix as instance identifier
        
        logger.info(f"[LAMBDA] Using naming from REQUEST: lambda_prefix={lambda_prefix}, ecr_repo={ecr_repo_name}, dynamodb={dynamodb_table}, prefix={prefix}")
        
        # Parse ECR URI and reconstruct if repo name doesn't match
        import re
        ecr_uri_repo_name = None
        if ecr_uri and 'amazonaws.com' in ecr_uri:
            ecr_match = re.match(r'(\d+)\.dkr\.ecr\.([^.]+)\.amazonaws\.com/([^:]+):(.+)', ecr_uri)
            if ecr_match:
                account_id, uri_region, ecr_uri_repo_name, image_tag = ecr_match.groups()
                logger.info(f"[LAMBDA] Parsed ECR URI - Account: {account_id}, Region: {uri_region}, Repo: {ecr_uri_repo_name}, Tag: {image_tag}")
                
                # [FIX] Do NOT override the repository name from the URI if it's provided.
                # The frontend or user might provide a specific URI (e.g. user-scoped) 
                # that differs from the global 'ecr_repo_name' config.
                # Only warn if there's a mismatch but proceed with the provided URI.
                if ecr_uri_repo_name != ecr_repo_name:
                    logger.info(f"[LAMBDA] ℹ️ Using ECR URI repo '{ecr_uri_repo_name}' (Config says '{ecr_repo_name}')")
                    # ecr_uri = f"{account_id}.dkr.ecr.{uri_region}.amazonaws.com/{ecr_repo_name}:{image_tag}"
                    # logger.info(f"[LAMBDA] New ECR URI: {ecr_uri}")

        session = get_boto3_session(access_key, secret_key, region)

        # Verify ECR image exists using configurable repo name
        ecr = session.client("ecr")
        logger.info(f"[LAMBDA] Verifying ECR image exists in repo: {ecr_repo_name}, tag: {ECR_IMAGE_TAG}, region: {region}")
        try:
            # First check if repository exists
            try:
                repo_response = ecr.describe_repositories(repositoryNames=[ecr_repo_name])
                logger.info(f"[LAMBDA] ✓ ECR repository '{ecr_repo_name}' exists in region {region}")
            except ClientError as repo_err:
                error_code = repo_err.response.get('Error', {}).get('Code', '')
                if error_code == 'RepositoryNotFoundException':
                    logger.error(f"[LAMBDA] ✗ ECR repository '{ecr_repo_name}' NOT FOUND in region {region}")
                    return jsonify({
                        'success': False,
                        'error': f'ECR repository "{ecr_repo_name}" not found in region {region}. Please create the repository first or check the repository name.'
                    }), 400
                else:
                    raise repo_err
            
            # Then check if image exists
            try:
                ecr.describe_images(
                    repositoryName=ecr_repo_name,
                    imageIds=[{"imageTag": ECR_IMAGE_TAG}],
                )
                logger.info(f"[LAMBDA] ✓ ECR image with tag '{ECR_IMAGE_TAG}' exists in repository '{ecr_repo_name}'")
            except ClientError as img_err:
                error_code = img_err.response.get('Error', {}).get('Code', '')
                if error_code == 'ImageNotFoundException':
                    logger.error(f"[LAMBDA] ✗ ECR image with tag '{ECR_IMAGE_TAG}' NOT FOUND in repository '{ecr_repo_name}'")
                    # Try to list available images to help user
                    try:
                        list_response = ecr.list_images(repositoryName=ecr_repo_name, maxResults=10)
                        available_tags = [img.get('imageTag') for img in list_response.get('imageIds', []) if img.get('imageTag')]
                        if available_tags:
                            return jsonify({
                                'success': False,
                                'error': f'ECR image with tag "{ECR_IMAGE_TAG}" not found in repository "{ecr_repo_name}". Available tags: {", ".join(available_tags[:5])}. Launch EC2 build box to build and push the image.'
                            }), 400
                        else:
                            return jsonify({
                                'success': False,
                                'error': f'ECR repository "{ecr_repo_name}" exists but contains no images. Launch EC2 build box, wait a few minutes for the build to complete, then try again.'
                            }), 400
                    except Exception as list_err:
                        logger.warning(f"[LAMBDA] Could not list images: {list_err}")
                        return jsonify({
                            'success': False,
                            'error': f'ECR image with tag "{ECR_IMAGE_TAG}" not found in repository "{ecr_repo_name}". Launch EC2 build box, wait a few minutes, then try again.'
                        }), 400
                else:
                    raise img_err
        except Exception as e:
            logger.error(f"[LAMBDA] Error verifying ECR image: {e}")
            logger.error(traceback.format_exc())
            return jsonify({
                'success': False,
                'error': f'Error verifying ECR image: {str(e)}'
            }), 500

        # Ensure IAM role with custom prefix
        lambda_role_name = f"{prefix}-lambda-role"
        logger.info(f"[LAMBDA] Creating/verifying IAM role: {lambda_role_name}")
        role_arn = ensure_lambda_role(session, lambda_role_name)

        # Environment variables (removed SFTP)
        # Use centralized DynamoDB in eu-west-1 (all Lambda functions save to same table)
        chromium_env = {
            "DYNAMODB_TABLE_NAME": dynamodb_table,  # DynamoDB table for password storage (configurable)
            "DYNAMODB_REGION": "eu-west-1",  # Centralized region - all Lambda functions use this
            "APP_PASSWORDS_S3_BUCKET": s3_bucket,
            "APP_PASSWORDS_S3_KEY": "app-passwords.txt",
            "INSTANCE_NAME": instance_name,  # Instance identifier for multi-tenant support
            "PARALLEL_BATCH_SIZE": "4",  # [CUSTOM] Increase parallel browsers inside Lambda to 4
        }
        
        # Add 2Captcha configuration if enabled
        twocaptcha_config = get_twocaptcha_config()
        logger.info(f"[2CAPTCHA] Retrieved config from database: enabled={twocaptcha_config.get('enabled') if twocaptcha_config else False}, has_api_key={bool(twocaptcha_config and twocaptcha_config.get('api_key'))}")
        
        if twocaptcha_config and twocaptcha_config.get('enabled') and twocaptcha_config.get('api_key'):
            chromium_env['TWOCAPTCHA_ENABLED'] = 'true'
            chromium_env['TWOCAPTCHA_API_KEY'] = twocaptcha_config.get('api_key', '')
            logger.info(f"[2CAPTCHA] ✓ 2Captcha feature ENABLED for automatic CAPTCHA solving")
            logger.info(f"[2CAPTCHA] API key length: {len(chromium_env['TWOCAPTCHA_API_KEY'])} characters")
        else:
            chromium_env['TWOCAPTCHA_ENABLED'] = 'false'
            chromium_env['TWOCAPTCHA_API_KEY'] = ''
            if not twocaptcha_config:
                logger.warning(f"[2CAPTCHA] ✗ 2Captcha config not found in database")
            elif not twocaptcha_config.get('enabled'):
                logger.warning(f"[2CAPTCHA] ✗ 2Captcha is disabled in database")
            elif not twocaptcha_config.get('api_key'):
                logger.warning(f"[2CAPTCHA] ✗ 2Captcha API key is empty in database")
            logger.info(f"[2CAPTCHA] 2Captcha feature disabled or not configured - Lambda will not solve CAPTCHAs")
        
        # Add Proxy configuration if enabled
        proxy_config = get_proxy_config()
        logger.info(f"[PROXY] Retrieved config from database: enabled={proxy_config.get('enabled') if proxy_config else False}, has_proxies={bool(proxy_config and proxy_config.get('proxies'))}")
        
        if proxy_config and proxy_config.get('enabled') and proxy_config.get('proxies'):
            chromium_env['PROXY_ENABLED'] = 'true'
            chromium_env['PROXY_LIST'] = proxy_config.get('proxies', '')
            # Count number of proxies
            proxy_count = len([line for line in proxy_config.get('proxies', '').strip().split('\n') if line.strip()])
            logger.info(f"[PROXY] ✓ Proxy feature ENABLED with {proxy_count} proxy/proxies")
            logger.info(f"[PROXY] Proxy list length: {len(chromium_env['PROXY_LIST'])} characters")
        else:
            chromium_env['PROXY_ENABLED'] = 'false'
            chromium_env['PROXY_LIST'] = ''
            if not proxy_config:
                logger.warning(f"[PROXY] ✗ Proxy config not found in database")
            elif not proxy_config.get('enabled'):
                logger.warning(f"[PROXY] ✗ Proxy is disabled in database")
            elif not proxy_config.get('proxies'):
                logger.warning(f"[PROXY] ✗ Proxy list is empty in database")
            logger.info(f"[PROXY] Proxy feature disabled or not configured - Lambda will not use proxies")


        # Calculate number of Lambda functions to create
        import math
        if create_multiple and user_count > 0 and users_per_function > 0:
            num_functions = math.ceil(user_count / users_per_function)  # Ceiling division
            num_functions = max(1, num_functions)  # At least 1 function
            logger.info(f"[LAMBDA] ========== LAMBDA CREATION CALCULATION ==========")
            logger.info(f"[LAMBDA] User count: {user_count}")
            logger.info(f"[LAMBDA] Users per function: {users_per_function}")
            logger.info(f"[LAMBDA] Calculation: {user_count} / {users_per_function} = {user_count / users_per_function}")
            logger.info(f"[LAMBDA] Ceiling result: {num_functions} function(s)")
            logger.info(f"[LAMBDA] Creating {num_functions} Lambda function(s) for {user_count} users ({users_per_function} users per function)")
            logger.info(f"[LAMBDA] =================================================")
        else:
            num_functions = 1
            logger.info(f"[LAMBDA] Creating single Lambda function")

        # Get all available AWS regions (geos) for distribution
        # [MULTI-REGION] Deploy Lambda to ALL regions where ECR image exists
        # The ECR push script replicates the image to all regions, so we can deploy globally
        # This enables invoking Lambdas from any region for better performance
        deploy_globally = data.get('deploy_globally', True)  # Default to global deployment
        
        # Full list of all available regions for global deployment
        GLOBAL_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]
        
        # Use global regions if deploy_globally is True, otherwise just the selected region
        if deploy_globally:
            # [IMPROVED] Validate ECR image existence in each region before adding to list
            # This ensures we don't try to create Lambdas in regions where the image hasn't replicated yet
            logger.info(f"[LAMBDA] Validating ECR image availability in {len(GLOBAL_GEO_REGIONS)} regions...")
            VALID_GEO_REGIONS = []
            
            # Helper to check image in a region
            def check_region_image(region_chk):
                try:
                    # Create session for this region
                    session_chk = boto3.Session(
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        region_name=region_chk
                    )
                    ecr_chk = session_chk.client('ecr')
                    
                    # Check if repo exists
                    try:
                        ecr_chk.describe_repositories(repositoryNames=[ecr_repo_name])
                    except ecr_chk.exceptions.RepositoryNotFoundException:
                        return None # Repo missing
                    except Exception:
                        return None # Other error/access denied
                        
                    # Check if ANY image exists (we assume if any image exists, it's usable, or we could check for 'latest')
                    try:
                        imgs = ecr_chk.list_images(repositoryName=ecr_repo_name, maxResults=1, filter={'tagStatus': 'TAGGED'})
                        if imgs.get('imageIds'):
                            return region_chk
                    except Exception:
                        return None
                        
                    return None
                except Exception as e:
                    logger.warning(f"[LAMBDA] Failed to check region {region_chk}: {e}")
                    return None

            # Check regions in parallel
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(check_region_image, r) for r in GLOBAL_GEO_REGIONS]
                for future in as_completed(futures):
                    res = future.result()
                    if res:
                        VALID_GEO_REGIONS.append(res)
            
            if not VALID_GEO_REGIONS:
                 logger.warning("[LAMBDA] ⚠️ No global regions had the ECR image! Falling back to selected region only.")
                 AVAILABLE_GEO_REGIONS = [region]
            else:
                 AVAILABLE_GEO_REGIONS = sorted(VALID_GEO_REGIONS)
                 # Ensure strict even distribution: 
                 # If we have valid regions [A, B, C] and 4 functions, we want A=2, B=1, C=1.
                 # The current logic handles this by iterating AVAILABLE_GEO_REGIONS cyclically.
                 
            logger.info(f"[LAMBDA] Global deployment enabled - Validated {len(AVAILABLE_GEO_REGIONS)} regions with images")
        else:
            AVAILABLE_GEO_REGIONS = [region]
            logger.info(f"[LAMBDA] Single region deployment - deploying only to {region}")
        
        
        # Distribute functions evenly across all available geos
        # Calculate how many functions each region should get for equal distribution
        functions_by_geo = {}  # {geo: [(function_number, function_name), ...]}
        created_functions = []
        
        # Calculate base functions per region and remainder
        base_functions_per_region = num_functions // len(AVAILABLE_GEO_REGIONS)
        remainder = num_functions % len(AVAILABLE_GEO_REGIONS)
        
        logger.info(f"[LAMBDA] Distribution calculation: {num_functions} functions across {len(AVAILABLE_GEO_REGIONS)} regions")
        logger.info(f"[LAMBDA] Base: {base_functions_per_region} per region, Remainder: {remainder} regions get +1")
        
        func_counter = 0
        for geo_index, geo in enumerate(AVAILABLE_GEO_REGIONS):
            # First 'remainder' regions get one extra function for equal distribution
            functions_in_this_geo = base_functions_per_region + (1 if geo_index < remainder else 0)
            
            if geo not in functions_by_geo:
                functions_by_geo[geo] = []
            
            geo_code = geo.replace('-', '')  # Remove dashes: us-east-1 -> useast1
            
            for i in range(functions_in_this_geo):
                func_num = func_counter + 1  # Function numbers start at 1
                
                if num_functions == 1:
                    function_name = lambda_prefix
                else:
                    function_name = f"{lambda_prefix}-{geo_code}-{func_num}"
                
                functions_by_geo[geo].append((func_num, function_name))
                created_functions.append(function_name)
                func_counter += 1
        
        logger.info("=" * 60)
        logger.info(f"[LAMBDA] Function Distribution Across Geos")
        logger.info(f"[LAMBDA] Total functions: {num_functions}")
        logger.info(f"[LAMBDA] Functions per geo:")
        for geo, func_list in sorted(functions_by_geo.items()):
            func_names = [name for _, name in func_list]
            logger.info(f"[LAMBDA]   - {geo}: {len(func_list)} function(s) {func_names}")
        logger.info("=" * 60)
        

        
        # Start background thread to create/update Lambda functions across geos
        # Use 900 seconds (15 minutes) timeout for batch processing (10 users can take 5-10 minutes)
        def create_lambdas_background(functions_by_geo_dict, access_key, secret_key, role_arn, timeout, env_vars, package_type, base_ecr_uri, ecr_repo_name=None, dynamodb_table=None, job_id=None):
            """
            Create Lambda functions distributed across geos.
            Each geo gets its own boto3 session and creates functions in its region.
            """
            # Use provided names or fall back to defaults
            if ecr_repo_name is None:
                ecr_repo_name = get_naming_config().get('ecr_repo_name', DEFAULT_ECR_REPO_NAME)
            if dynamodb_table is None:
                dynamodb_table = get_naming_config().get('dynamodb_table', DEFAULT_DYNAMODB_TABLE)
            success_count = 0
            failure_count = 0
            errors_by_geo = {}
            successfully_created_functions = []  # Track only successfully created functions
            
            def update_job_status():
                """Update the job status in the tracking dictionary"""
                if job_id:
                    with lambda_creation_lock:
                        if job_id in lambda_creation_jobs:
                            lambda_creation_jobs[job_id]['success_count'] = success_count
                            lambda_creation_jobs[job_id]['failure_count'] = failure_count
                            lambda_creation_jobs[job_id]['errors'] = errors_by_geo.copy()
            
            try:
                logger.info("=" * 60)
                logger.info(f"[LAMBDA] Starting PARALLEL Lambda creation across {len(functions_by_geo_dict)} geo(s)")
                logger.info("=" * 60)
                
                from concurrent.futures import ThreadPoolExecutor, as_completed
                
                def create_functions_in_geo(geo, func_list):
                    """Create Lambda functions in a specific geo region (thread-safe)"""
                    geo_errors = []
                    geo_success = 0
                    geo_failure = 0
                    
                    logger.info(f"[LAMBDA] [{geo}] ===== Starting creation of {len(func_list)} function(s) in region {geo} =====")
                    
                    try:
                        # Create boto3 session for this geo's region
                        geo_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=geo
                        )
                        logger.info(f"[LAMBDA] [{geo}] ✓ Created boto3 session for region {geo}")
                        
                        # Verify credentials work by testing STS
                        try:
                            sts = geo_session.client('sts')
                            identity = sts.get_caller_identity()
                            logger.info(f"[LAMBDA] [{geo}] ✓ Credentials verified. Account: {identity.get('Account')}")
                        except Exception as sts_err:
                            logger.error(f"[LAMBDA] [{geo}] ✗ Credential verification failed: {sts_err}")
                            geo_errors.append(f"Credential verification failed: {sts_err}")
                            geo_failure = len(func_list)
                            return {
                                'geo': geo,
                                'success_count': 0,
                                'failure_count': geo_failure,
                                'errors': geo_errors
                            }
                        
                        # CRITICAL: ECR repositories are region-specific.
                        # Lambda CANNOT pull ECR images from other regions directly.
                        # Solution: Only create functions in regions where ECR image exists,
                        # OR use the same region as the ECR image for all functions.
                        
                        import re
                        geo_ecr_uri = None
                        # Use configurable ECR repo name from parameter
                        repo_name = ecr_repo_name  # From function parameter
                        image_tag = ECR_IMAGE_TAG
                        base_region = None
                        
                        # Extract account ID and base region from base ECR URI
                        # Format: account_id.dkr.ecr.region.amazonaws.com/repo:tag
                        try:
                            ecr_match = re.match(r'(\d+)\.dkr\.ecr\.([^.]+)\.amazonaws\.com/([^:]+):(.+)', base_ecr_uri)
                            if ecr_match:
                                account_id, base_region, parsed_repo_name, image_tag = ecr_match.groups()
                                # Only override repo_name if it wasn't provided or set
                                if not repo_name:
                                    repo_name = parsed_repo_name
                                else:
                                    logger.info(f"[LAMBDA] [{geo}] Using configured ECR repo: {repo_name} (ignoring parsed: {parsed_repo_name})")
                                logger.info(f"[LAMBDA] [{geo}] Parsed ECR URI - Account: {account_id}, Base Region: {base_region}, Repo: {repo_name}, Tag: {image_tag}")
                            else:
                                # Fallback: try to get account ID from STS
                                sts = geo_session.client('sts')
                                account_id = sts.get_caller_identity()['Account']
                                logger.warning(f"[LAMBDA] [{geo}] Could not parse ECR URI, using account ID from STS: {account_id}")
                        except Exception as parse_err:
                            logger.error(f"[LAMBDA] [{geo}] Could not parse ECR URI or get account ID: {parse_err}")
                            geo_errors.append(f"Could not determine ECR configuration: {parse_err}")
                            geo_failure = len(func_list)
                            return {
                                'geo': geo,
                                'success_count': 0,
                                'failure_count': geo_failure,
                                'errors': geo_errors
                            }
                        
                        # Construct ECR URI for this region
                        if geo == base_region:
                            geo_ecr_uri = base_ecr_uri
                        else:
                            geo_ecr_uri = f"{account_id}.dkr.ecr.{geo}.amazonaws.com/{repo_name}:{image_tag}"
                        
                        logger.info(f"[LAMBDA] [{geo}] Checking for ECR image: {geo_ecr_uri}")
                        
                        try:
                            ecr_client = geo_session.client('ecr')
                            
                            # 1. Check if repository exists
                            try:
                                ecr_client.describe_repositories(repositoryNames=[repo_name])
                            except ClientError as repo_err:
                                error_code = repo_err.response.get('Error', {}).get('Code', '')
                                if error_code in ['RepositoryNotFoundException', 'ResourceNotFoundException']:
                                    # [FIX] Do not return early. Warn and proceed to let create_function try.
                                    logger.warning(f"[LAMBDA] [{geo}] ⚠️ ECR repository verification failed: Not Found. Proceeding with creation attempt anyway.")
                                else:
                                    logger.warning(f"[LAMBDA] [{geo}] ⚠️ ECR repository check error: {repo_err}. Proceeding...")
                                # Do NOT raise repo_err, just proceed

                            # 2. Check for image using list_images (more reliable)
                            image_found = False
                            try:
                                logger.info(f"[LAMBDA] [{geo}] DEBUG: Listing images in {repo_name}...")
                                list_response = ecr_client.list_images(
                                    repositoryName=repo_name,
                                    maxResults=100
                                )
                                image_ids = list_response.get('imageIds', [])
                                logger.info(f"[LAMBDA] [{geo}] DEBUG: Found {len(image_ids)} images. Checking for tag '{image_tag}'...")
                                
                                for img in image_ids:
                                    img_tag = img.get('imageTag')
                                    if img_tag == image_tag:
                                        image_found = True
                                        logger.info(f"[LAMBDA] [{geo}] DEBUG: Found matching tag in list_images: {img_tag}")
                                        break
                                
                                if not image_found:
                                    logger.info(f"[LAMBDA] [{geo}] DEBUG: Tag '{image_tag}' NOT found in list_images results.")
                                    
                            except Exception as list_err:
                                logger.warning(f"[LAMBDA] [{geo}] list_images failed, falling back to describe_images: {list_err}")

                            # 3. Fallback/Confirmation with describe_images
                            if not image_found:
                                logger.info(f"[LAMBDA] [{geo}] DEBUG: Attempting fallback describe_images check...")
                                try:
                                    ecr_client.describe_images(
                                        repositoryName=repo_name,
                                        imageIds=[{"imageTag": image_tag}],
                                    )
                                    image_found = True
                                    logger.info(f"[LAMBDA] [{geo}] DEBUG: describe_images SUCCEEDED (image exists).")
                                except ClientError as desc_err:
                                    error_code = desc_err.response.get('Error', {}).get('Code', '')
                                    logger.info(f"[LAMBDA] [{geo}] DEBUG: describe_images FAILED with {error_code}.")
                                    if error_code in ['ImageNotFoundException', 'ResourceNotFoundException']:
                                        pass # Still not found
                                    else:
                                        raise desc_err

                            if not image_found:
                                logger.warning(f"[LAMBDA] [{geo}] ⚠️ ECR image tag '{image_tag}' not found in {geo}. Skipping this region.")
                                return {
                                    'geo': geo,
                                    'success_count': 0,
                                    'failure_count': 0,
                                    'errors': []
                                }

                            logger.info(f"[LAMBDA] [{geo}] ✓ ECR image exists in region {geo}")

                        except Exception as ecr_check_err:
                            logger.error(f"[LAMBDA] [{geo}] ✗ ECR verification error: {ecr_check_err}")
                            geo_errors.append(f"ECR verification failed: {ecr_check_err}")
                            geo_failure = len(func_list)
                            return {
                                'geo': geo,
                                'success_count': 0,
                                'failure_count': geo_failure,
                                'errors': geo_errors
                            }
                        
                        logger.info(f"[LAMBDA] [{geo}] ✓ Using ECR URI: {geo_ecr_uri}")
                        
                        # Ensure IAM role exists (IAM is global, but we use the session for consistency)
                        try:
                            geo_role_arn = ensure_lambda_role(geo_session)
                            logger.info(f"[LAMBDA] [{geo}] ✓ IAM role verified/created: {geo_role_arn}")
                        except Exception as role_err:
                            logger.error(f"[LAMBDA] [{geo}] ✗ IAM role creation failed: {role_err}")
                            logger.error(traceback.format_exc())
                            # Try using the provided role ARN (should work since IAM is global)
                            geo_role_arn = role_arn
                            logger.warning(f"[LAMBDA] [{geo}] Using fallback role ARN: {geo_role_arn}")
                        
                        # Create each function in this geo
                        # Re-initialize counters (they were initialized at function start but need to be here too)
                        geo_failures = []
                        logger.info(f"[LAMBDA] [{geo}] ===== About to create {len(func_list)} function(s) in {geo} =====")
                        for func_num, function_name in func_list:
                            logger.info(f"[LAMBDA] [{geo}] Function #{func_num}: {function_name}")
                        logger.info(f"[LAMBDA] [{geo}] =========================================================")
                        
                        for func_num, function_name in func_list:
                            try:
                                logger.info("=" * 60)
                                logger.info(f"[LAMBDA] [{geo}] ===== CREATING FUNCTION {func_num}/{len(func_list)}: {function_name} =====")
                                logger.info(f"[LAMBDA] [{geo}] Using ECR URI: {geo_ecr_uri}")
                                logger.info(f"[LAMBDA] [{geo}] Using ECR repo name: {repo_name}")
                                logger.info(f"[LAMBDA] [{geo}] Using image tag: {image_tag}")
                                logger.info(f"[LAMBDA] [{geo}] Using role ARN: {geo_role_arn}")
                                logger.info("=" * 60)
                                
                                create_or_update_lambda(
                                    session=geo_session,
                                    function_name=function_name,
                                    role_arn=geo_role_arn,
                                    timeout=timeout,
                                    env_vars=env_vars,
                                    package_type=package_type,
                                    image_uri=geo_ecr_uri,
                                )
                                
                                # CRITICAL: Verify function was actually created and is in Active state
                                lam_client = geo_session.client("lambda")
                                try:
                                    func_info = lam_client.get_function(FunctionName=function_name)
                                    func_state = func_info.get('Configuration', {}).get('State', 'Unknown')
                                    
                                    if func_state == 'Active':
                                        logger.info(f"[LAMBDA] [{geo}] ✓✓✓ SUCCESS: Created/Updated Lambda: {function_name} (State: {func_state})")
                                        geo_success += 1
                                        successfully_created_functions.append(function_name)  # Track successful creation
                                        update_job_status()
                                    else:
                                        logger.warning(f"[LAMBDA] [{geo}] ⚠️ Function created but not Active: {function_name} (State: {func_state})")
                                        # Wait a bit and check again
                                        time.sleep(5)
                                        func_info = lam_client.get_function(FunctionName=function_name)
                                        func_state = func_info.get('Configuration', {}).get('State', 'Unknown')
                                        if func_state == 'Active':
                                            logger.info(f"[LAMBDA] [{geo}] ✓✓✓ SUCCESS: Lambda is now Active: {function_name}")
                                            geo_success += 1
                                            successfully_created_functions.append(function_name)  # Track successful creation
                                            update_job_status()
                                        else:
                                            raise Exception(f"Function created but not in Active state. Current state: {func_state}")
                                    
                                except ClientError as verify_err:
                                    error_code = verify_err.response.get('Error', {}).get('Code', '')
                                    if error_code == 'ResourceNotFoundException':
                                        # Function was not actually created despite no exception
                                        raise Exception(f"Function creation appeared to succeed but function does not exist: {function_name}")
                                    else:
                                        raise
                                
                            except Exception as func_error:
                                error_msg = str(func_error)
                                logger.error(f"[LAMBDA] [{geo}] ✗✗✗ FAILED to create/update {function_name}: {error_msg}")
                                
                                # Check if it's an ECR image error
                                if 'ECR image not found' in error_msg or 'is not valid' in error_msg or 'Source image' in error_msg:
                                    logger.error(f"[LAMBDA] [{geo}] ⚠️ ECR IMAGE MISSING in region {geo}")
                                    logger.error(f"[LAMBDA] [{geo}] Solution: Use 'Push ECR to All Regions' button to push image to {geo}")
                                
                                logger.error(traceback.format_exc())
                                geo_failure += 1
                                geo_failures.append(f"{function_name}: {error_msg}")
                                update_job_status()
                        
                        if geo_failures:
                            geo_errors = geo_failures
                        
                        logger.info(f"[LAMBDA] [{geo}] ===== Completed: {geo_success}/{len(func_list)} success, {len(geo_failures)} failed =====")
                    
                    except Exception as geo_error:
                        error_msg = str(geo_error)
                        logger.error(f"[LAMBDA] [{geo}] ✗✗✗ CRITICAL ERROR processing geo {geo}: {error_msg}")
                        logger.error(traceback.format_exc())
                        geo_errors.append(f"Geo processing failed: {error_msg}")
                        geo_failure = len(func_list)
                    
                    return {
                        'geo': geo,
                        'success_count': geo_success,
                        'failure_count': geo_failure,
                        'errors': geo_errors
                    }
                
                # --- EXPLICIT PRE-CHECK PHASE ---
                logger.info("=" * 60)
                logger.info(f"[LAMBDA] PRE-CHECK: Verifying ECR images in {len(functions_by_geo_dict)} regions...")
                logger.info("=" * 60)
                
                # Parse ECR details once - use configurable repo name
                import re
                pc_repo_name = ecr_repo_name  # Use configurable name from parameter
                pc_image_tag = ECR_IMAGE_TAG
                pc_account_id = None
                
                try:
                    pc_match = re.match(r'(\d+)\.dkr\.ecr\.([^.]+)\.amazonaws\.com/([^:]+):(.+)', base_ecr_uri)
                    if pc_match:
                        pc_account_id, _, parsed_repo_name, parsed_image_tag = pc_match.groups()
                        # Use parsed repo name if it matches, otherwise keep configurable name
                        if parsed_repo_name == ecr_repo_name or not pc_repo_name:
                            pc_repo_name = parsed_repo_name
                        pc_image_tag = parsed_image_tag
                except Exception:
                    pass
                
                logger.info(f"[LAMBDA] Using ECR repo name: {pc_repo_name} (configurable: {ecr_repo_name})")



                def verify_geo_image(geo):
                    """Check if ECR image exists in the region (Robust Check)"""
                    try:
                        pc_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=geo
                        )
                        pc_ecr = pc_session.client('ecr')
                        
                        # 1. Check Repo
                        try:
                            pc_ecr.describe_repositories(repositoryNames=[pc_repo_name])
                        except ClientError as re:
                            if re.response.get('Error', {}).get('Code') in ['RepositoryNotFoundException', 'ResourceNotFoundException']:
                                # Return True anyway to allow create_function to try
                                return geo, True, "Repository verification failed: Not Found (Proceeding)"
                            # Ignore other errors and proceed
                            pass
                            
                        # 2. List Images
                        found = False
                        try:
                            list_resp = pc_ecr.list_images(repositoryName=pc_repo_name, maxResults=100)
                            for img in list_resp.get('imageIds', []):
                                if img.get('imageTag') == pc_image_tag:
                                    found = True
                                    break
                        except Exception:
                            pass
                            
                        # 3. Describe Images (Fallback)
                        if not found:
                            try:
                                pc_ecr.describe_images(repositoryName=pc_repo_name, imageIds=[{"imageTag": pc_image_tag}])
                                found = True
                            except ClientError:
                                pass
                        
                        if found:
                            return geo, True, "OK"
                        else:
                            # Even if not found by our check, return True to attempt creation
                            return geo, True, f"Image tag '{pc_image_tag}' verification failed (Proceeding)"
                            
                    except Exception as e:
                        return geo, True, f"Verification error: {e} (Proceeding)"

                valid_geos = set()
                skipped_geos = []
                
                with ThreadPoolExecutor(max_workers=20) as pc_executor:
                    pc_futures = {pc_executor.submit(verify_geo_image, geo): geo for geo in functions_by_geo_dict.keys()}
                    
                    for future in as_completed(pc_futures):
                        geo, is_valid, reason = future.result()
                        if is_valid:
                            valid_geos.add(geo)
                            logger.info(f"[LAMBDA] [PRE-CHECK] [{geo}] ✓ Image verified")
                        else:
                            skipped_geos.append(f"{geo} ({reason})")
                            logger.warning(f"[LAMBDA] [PRE-CHECK] [{geo}] ⚠️ Image MISSING: {reason}")

                # Filter and Redistribute
                original_count = len(functions_by_geo_dict)
                
                # 1. Identify valid and skipped regions
                valid_geos_list = sorted(list(valid_geos))
                skipped_geos_list = [geo for geo in functions_by_geo_dict.keys() if geo not in valid_geos]
                
                # 2. Collect orphaned functions from skipped regions
                orphaned_functions = []
                for geo in skipped_geos_list:
                    orphaned_functions.extend(functions_by_geo_dict[geo])
                
                # 3. Redistribute orphaned functions to valid regions EQUALLY
                if orphaned_functions and valid_geos_list:
                    logger.info(f"[LAMBDA] [REDISTRIBUTE] Found {len(orphaned_functions)} functions from skipped regions to redistribute.")
                    
                    # Calculate equal distribution for orphaned functions
                    total_orphaned = len(orphaned_functions)
                    base_orphaned_per_region = total_orphaned // len(valid_geos_list)
                    orphaned_remainder = total_orphaned % len(valid_geos_list)
                    
                    logger.info(f"[LAMBDA] [REDISTRIBUTE] Distributing {total_orphaned} orphaned functions across {len(valid_geos_list)} valid regions")
                    logger.info(f"[LAMBDA] [REDISTRIBUTE] Base: {base_orphaned_per_region} per region, Remainder: {orphaned_remainder} regions get +1")
                    
                    orphaned_index = 0
                    for geo_index, geo in enumerate(valid_geos_list):
                        # First 'orphaned_remainder' regions get one extra orphaned function
                        orphaned_in_this_geo = base_orphaned_per_region + (1 if geo_index < orphaned_remainder else 0)
                        
                        for _ in range(orphaned_in_this_geo):
                            if orphaned_index < len(orphaned_functions):
                                func_num, old_name = orphaned_functions[orphaned_index]
                                
                                # Generate new name for the target region using configurable prefix
                                geo_code = geo.replace('-', '')
                                new_name = f"{lambda_prefix}-{geo_code}-{func_num}"
                                
                                # Add to the target region's list
                                functions_by_geo_dict[geo].append((func_num, new_name))
                                
                                logger.info(f"[LAMBDA] [REDISTRIBUTE] Moved function #{func_num} from skipped region to {geo} (Renamed: {old_name} -> {new_name})")
                                orphaned_index += 1
                
                # 4. Remove skipped regions from the dictionary
                functions_by_geo_dict = {k: v for k, v in functions_by_geo_dict.items() if k in valid_geos}
                
                logger.info("=" * 60)
                logger.info(f"[LAMBDA] PRE-CHECK COMPLETED")
                logger.info(f"[LAMBDA]   Total requested: {original_count}")
                logger.info(f"[LAMBDA]   ✓ Valid regions: {len(valid_geos)}")
                logger.info(f"[LAMBDA]   ⚠️ Skipped regions: {len(skipped_geos)}")
                if skipped_geos:
                    logger.info(f"[LAMBDA]   Skipped list: {', '.join(skipped_geos)}")
                logger.info("=" * 60)
                
                if not functions_by_geo_dict:
                    logger.error("[LAMBDA] ✗✗✗ STOPPING: No valid regions found with ECR images.")
                    # Update job status to failed
                    if job_id:
                        with lambda_creation_lock:
                            if job_id in lambda_creation_jobs:
                                lambda_creation_jobs[job_id]['errors'] = {'ALL': ['No valid regions found with ECR images']}
                    return []

                # Execute Lambda creation in parallel across all geos
                max_workers = min(len(functions_by_geo_dict), 20)  # Increased to 20 for better parallelization
                logger.info(f"[LAMBDA] Using ThreadPoolExecutor with {max_workers} workers for {len(functions_by_geo_dict)} regions")
                
                processed_geos = set()
                failed_geos = {}  # Track failed geos for retry
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # Submit all geo creation tasks
                    future_to_geo = {
                        executor.submit(create_functions_in_geo, geo, func_list): geo
                        for geo, func_list in functions_by_geo_dict.items()
                    }
                    
                    # Process results as they complete
                    for future in as_completed(future_to_geo):
                        geo = future_to_geo[future]
                        try:
                            result = future.result()
                            processed_geos.add(geo)
                            success_count += result['success_count']
                            failure_count += result['failure_count']
                            if result.get('errors'):
                                errors_by_geo[result['geo']] = result['errors']
                                failed_geos[geo] = result['errors']
                                # Log errors clearly for each geo
                                logger.error(f"[LAMBDA] [{geo}] ✗✗✗ FAILED: {result['failure_count']} function(s) failed")
                                for error in result['errors']:
                                    logger.error(f"[LAMBDA] [{geo}]   ERROR: {error}")
                            else:
                                logger.info(f"[LAMBDA] [{geo}] ✓✓✓ SUCCESS: Created {result['success_count']} function(s)")
                            logger.info(f"[LAMBDA] [{geo}] Thread completed: {result['success_count']} success, {result['failure_count']} failures")
                            update_job_status()
                        except Exception as thread_err:
                            logger.error(f"[LAMBDA] [{geo}] Thread execution error: {thread_err}")
                            logger.error(traceback.format_exc())
                            errors_by_geo[geo] = [f"Thread execution error: {thread_err}"]
                            failed_geos[geo] = [f"Thread execution error: {thread_err}"]
                            failure_count += len(functions_by_geo_dict[geo])
                            processed_geos.add(geo)  # Mark as processed even if failed
                            update_job_status()
                    
                    # CRITICAL: Verify ALL geos were processed
                    expected_geos = set(functions_by_geo_dict.keys())
                    missing_geos = expected_geos - processed_geos
                    
                    if missing_geos:
                        logger.error("=" * 60)
                        logger.error(f"[LAMBDA] ✗✗✗ CRITICAL: {len(missing_geos)} geo(s) were NOT processed!")
                        logger.error(f"[LAMBDA] Missing geos: {sorted(missing_geos)}")
                        logger.error("=" * 60)
                        
                        # Retry missing geos
                        if missing_geos:
                            logger.info(f"[LAMBDA] Retrying {len(missing_geos)} missing geo(s)...")
                            with ThreadPoolExecutor(max_workers=min(len(missing_geos), 10)) as retry_executor:
                                retry_futures = {
                                    retry_executor.submit(create_functions_in_geo, geo, functions_by_geo_dict[geo]): geo
                                    for geo in missing_geos
                                }
                                for future in as_completed(retry_futures):
                                    retry_geo = retry_futures[future]
                                    try:
                                        retry_result = future.result()
                                        processed_geos.add(retry_geo)
                                        success_count += retry_result['success_count']
                                        failure_count += retry_result['failure_count']
                                        if retry_result.get('errors'):
                                            errors_by_geo[retry_geo] = retry_result['errors']
                                            logger.error(f"[LAMBDA] [{retry_geo}] ✗ Retry failed")
                                        else:
                                            logger.info(f"[LAMBDA] [{retry_geo}] ✓ Retry successful")
                                        update_job_status()
                                    except Exception as retry_err:
                                        logger.error(f"[LAMBDA] [{retry_geo}] ✗ Retry exception: {retry_err}")
                                        failure_count += len(functions_by_geo_dict[retry_geo])
                                        if retry_geo not in errors_by_geo:
                                            errors_by_geo[retry_geo] = []
                                        errors_by_geo[retry_geo].append(f"Retry exception: {str(retry_err)}")
                                        processed_geos.add(retry_geo)
                                        update_job_status()
                    
                    # Final verification
                    final_processed = processed_geos
                    if final_processed != expected_geos:
                        logger.error(f"[LAMBDA] ✗✗✗ FINAL CHECK FAILED: Still missing {len(expected_geos - final_processed)} geo(s)")
                        logger.error(f"[LAMBDA] Still missing: {sorted(expected_geos - final_processed)}")
                    else:
                        logger.info(f"[LAMBDA] ✓✓✓ FINAL CHECK PASSED: All {len(expected_geos)} expected geos were processed")
                
                logger.info("=" * 60)
                logger.info(f"[LAMBDA] ===== BACKGROUND LAMBDA CREATION COMPLETED =====")
                logger.info(f"[LAMBDA] Total Success: {success_count}, Total Failed: {failure_count}")
                logger.info(f"[LAMBDA] Successfully created functions: {len(successfully_created_functions)}")
                if successfully_created_functions:
                    logger.info(f"[LAMBDA] ✓✓✓ CREATED FUNCTION NAMES:")
                    for func_name in successfully_created_functions:
                        logger.info(f"[LAMBDA]   - {func_name}")
                else:
                    logger.error(f"[LAMBDA] ✗✗✗ NO FUNCTIONS WERE CREATED!")
                    logger.error(f"[LAMBDA] This usually means:")
                    logger.error(f"[LAMBDA]   1. ECR images were not found in any region")
                    logger.error(f"[LAMBDA]   2. All regions were skipped during pre-check")
                    logger.error(f"[LAMBDA]   3. All function creations failed (check errors below)")
                if errors_by_geo:
                    logger.error(f"[LAMBDA] Errors by geo:")
                    for geo, errors in errors_by_geo.items():
                        logger.error(f"[LAMBDA]   {geo}: {errors}")
                logger.info("=" * 60)
                
                # Final job status update - include successfully created functions
                if job_id:
                    with lambda_creation_lock:
                        if job_id in lambda_creation_jobs:
                            lambda_creation_jobs[job_id]['successfully_created_functions'] = successfully_created_functions
                            lambda_creation_jobs[job_id]['total_functions'] = len(successfully_created_functions)
                
                update_job_status()
                
                # Return successfully created functions for use by caller
                return successfully_created_functions
                
            except Exception as bg_error:
                logger.error(f"[LAMBDA] ✗✗✗ CRITICAL: Background Lambda creation error: {bg_error}")
                logger.error(traceback.format_exc())
        
        # Create a job ID for tracking Lambda creation
        creation_job_id = f"lambda_creation_{int(time.time())}"
        with lambda_creation_lock:
            lambda_creation_jobs[creation_job_id] = {
                'status': 'processing',
                'total_functions': len(created_functions),
                'success_count': 0,
                'failure_count': 0,
                'functions_by_geo': {geo: [name for _, name in func_list] for geo, func_list in functions_by_geo.items()},
                'errors': {},
                'started_at': time.time()
            }
        
        # Start background thread
        # Use 900 seconds (15 minutes) - AWS Lambda maximum timeout
        # This allows processing up to 10 users per batch (each user takes ~30-60 seconds)
        def create_lambdas_with_tracking(**kwargs):
            job_id = kwargs.get('job_id')
            try:
                create_lambdas_background(**kwargs)
                # Update job status on completion
                with lambda_creation_lock:
                    if job_id and job_id in lambda_creation_jobs:
                        if lambda_creation_jobs[job_id]['status'] == 'processing':
                            lambda_creation_jobs[job_id]['status'] = 'completed'
                            lambda_creation_jobs[job_id]['completed_at'] = time.time()
            except Exception as e:
                logger.error(f"[LAMBDA] Critical error in background thread: {e}")
                logger.error(traceback.format_exc())
                with lambda_creation_lock:
                    if job_id and job_id in lambda_creation_jobs:
                        lambda_creation_jobs[job_id]['status'] = 'failed'
                        lambda_creation_jobs[job_id]['error'] = str(e)
        
        # Execute ASYNCHRONOUSLY
        timeout = 900  # Default timeout 15 minutes
        logger.info(f"[LAMBDA] Starting background thread for job {creation_job_id}")
        threading.Thread(
            target=create_lambdas_with_tracking,
            kwargs={
                'functions_by_geo_dict': functions_by_geo, 
                'access_key': access_key,
                'secret_key': secret_key, 
                'role_arn': role_arn, 
                'timeout': timeout, 
                'env_vars': chromium_env, 
                'package_type': 'Image', 
                'base_ecr_uri': ecr_uri,
                'ecr_repo_name': ecr_repo_name,
                'dynamodb_table': dynamodb_table,
                'job_id': creation_job_id
            },
            daemon=True
        ).start()
        
        # Build summary message
        geo_summary = []
        for geo, func_list in sorted(functions_by_geo.items()):
            func_names = [name for _, name in func_list]
            geo_summary.append(f"{geo}: {len(func_list)} function(s)")
        
        message = f'Started creating/updating {len(created_functions)} Lambda function(s) distributed across {len(functions_by_geo)} geo(s).'
        if create_multiple:
            message += f' (for {user_count} users, {users_per_function} users per function).'
        message += f' Distribution: {"; ".join(geo_summary)}. Functions are being created in the background.'
        message += f' ⚠️ IMPORTANT: Functions will FAIL to create in regions where the ECR image is missing. Check the creation status for actual results and errors.'

        return jsonify({
            'success': True,
            'message': message,
            'functions_created': created_functions,
            'num_functions': len(created_functions),
            'functions_by_geo': {geo: [name for _, name in func_list] for geo, func_list in functions_by_geo.items()},
            'creation_job_id': creation_job_id,
            'debug_info': {
                'users_per_function_received': users_per_function,
                'user_count': user_count,
                'expected_functions': math.ceil(user_count / users_per_function) if user_count > 0 and users_per_function > 0 else 1,
                'actual_functions_planned': len(created_functions)
            },
            'note': 'Lambda functions are being created/updated in the background across multiple AWS regions. This may take a few minutes. Check creation status using the job ID.'
        })
    except Exception as e:
        logger.error(f"Error creating Lambda: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/check-aws-limits', methods=['POST'])
@login_required
def check_aws_limits():
    """Check ALL AWS limits that could affect Lambda concurrency"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        lam = session.client("lambda")
        service_quotas = session.client("service-quotas", region_name=region)
        
        limits_info = {
            'lambda_function': {},
            'account_limits': {},
            'service_quotas': {},
            'recommendations': []
        }
        
        # Get configurable lambda prefix for multi-tenant support
        naming_config_limits = get_naming_config()
        lambda_prefix_limits = naming_config_limits['production_lambda_name']
        
        # 1. Check Lambda Function Concurrency Settings
        try:
            func_config = lam.get_function_configuration(FunctionName=lambda_prefix_limits)
            limits_info['lambda_function']['name'] = lambda_prefix_limits
            limits_info['lambda_function']['state'] = func_config.get('State', 'Unknown')
            limits_info['lambda_function']['state_reason'] = func_config.get('StateReason', 'N/A')
            
            # Check Reserved Concurrency
            try:
                concurrency_config = lam.get_function_concurrency(FunctionName=lambda_prefix_limits)
                reserved = concurrency_config.get('ReservedConcurrentExecutions')
                limits_info['lambda_function']['reserved_concurrency'] = reserved
                if reserved and reserved < 1000:
                    limits_info['recommendations'].append(
                        f"⚠️ CRITICAL: Lambda has Reserved Concurrency = {reserved}. This limits concurrent executions to {reserved}!"
                    )
            except lam.exceptions.ResourceNotFoundException:
                limits_info['lambda_function']['reserved_concurrency'] = None
                limits_info['lambda_function']['reserved_concurrency_status'] = "Unreserved (Good - uses account limit)"
            
            # Check Provisioned Concurrency
            try:
                prov_configs = lam.list_provisioned_concurrency_configs(FunctionName=lambda_prefix_limits)
                if prov_configs.get('ProvisionedConcurrencyConfigs'):
                    limits_info['lambda_function']['provisioned_concurrency'] = prov_configs['ProvisionedConcurrencyConfigs']
                    limits_info['recommendations'].append(
                        "⚠️ Provisioned Concurrency is set (this doesn't limit, but costs money)"
                    )
                else:
                    limits_info['lambda_function']['provisioned_concurrency'] = None
            except Exception as e:
                limits_info['lambda_function']['provisioned_concurrency_error'] = str(e)
                
        except lam.exceptions.ResourceNotFoundException:
            limits_info['lambda_function']['error'] = f"Lambda function {PRODUCTION_LAMBDA_NAME} not found"
            limits_info['recommendations'].append("❌ Lambda function does not exist. Create it first.")
        except Exception as e:
            limits_info['lambda_function']['error'] = str(e)
        
        # 2. Check Account-Level Limits
        try:
            account_settings = lam.get_account_settings()
            account_limits = account_settings.get('AccountLimit', {})
            limits_info['account_limits']['total_concurrent_executions'] = account_limits.get('TotalCodeSize', 'N/A')
            limits_info['account_limits']['unreserved_concurrent_executions'] = account_limits.get('UnreservedConcurrentExecutions', 'N/A')
            
            # This is the KEY limit!
            unreserved = account_limits.get('UnreservedConcurrentExecutions')
            if unreserved and unreserved < 1000:
                limits_info['recommendations'].append(
                    f"⚠️ CRITICAL: Account Unreserved Concurrent Executions = {unreserved}. This is the hard limit!"
                )
            elif unreserved:
                limits_info['account_limits']['status'] = f"✅ Account limit is {unreserved} (sufficient for 1000+ users)"
        except Exception as e:
            limits_info['account_limits']['error'] = str(e)
            limits_info['recommendations'].append(f"Could not check account limits: {e}")
        
        # 3. Check Service Quotas (if available)
        try:
            # Try to get Lambda concurrent executions quota
            quota_code = "L-B99A9384"  # Lambda concurrent executions quota code
            try:
                quota = service_quotas.get_service_quota(
                    ServiceCode='lambda',
                    QuotaCode=quota_code
                )
                quota_value = quota['Quota']['Value']
                limits_info['service_quotas']['lambda_concurrent_executions'] = quota_value
                if quota_value < 1000:
                    limits_info['recommendations'].append(
                        f"⚠️ Service Quota limits Lambda to {quota_value} concurrent executions. Request increase via AWS Support."
                    )
            except service_quotas.exceptions.NoSuchResourceException:
                limits_info['service_quotas']['lambda_concurrent_executions'] = "Not found (using default)"
            except Exception as e:
                limits_info['service_quotas']['error'] = str(e)
        except Exception as e:
            limits_info['service_quotas']['error'] = f"Service Quotas API not available: {e}"
        
        # 4. Check current concurrent executions (if possible)
        try:
            # Get function metrics
            cloudwatch = session.client('cloudwatch', region_name=region)
            end_time = time.time()
            start_time = end_time - 300  # Last 5 minutes
            
            metrics = cloudwatch.get_metric_statistics(
                Namespace='AWS/Lambda',
                MetricName='ConcurrentExecutions',
                Dimensions=[
                    {'Name': 'FunctionName', 'Value': PRODUCTION_LAMBDA_NAME}
                ],
                StartTime=time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(start_time)),
                EndTime=time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(end_time)),
                Period=60,
                Statistics=['Maximum']
            )
            
            if metrics.get('Datapoints'):
                max_concurrent = max([dp['Maximum'] for dp in metrics['Datapoints']])
                limits_info['lambda_function']['recent_max_concurrent'] = max_concurrent
                if max_concurrent <= 10:
                    limits_info['recommendations'].append(
                        f"⚠️ Recent max concurrent executions was {max_concurrent} (confirms 10-user limit)"
                    )
        except Exception as e:
            limits_info['metrics_error'] = str(e)
        
        return jsonify({
            'success': True,
            'limits': limits_info,
            'summary': f"Found {len(limits_info['recommendations'])} potential issues"
        })
        
    except Exception as e:
        logger.error(f"Error checking AWS limits: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/request-quota-increase', methods=['POST'])
@login_required
def request_quota_increase():
    """Request Lambda concurrent executions quota increase"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        requested_limit = data.get('requested_limit', 1000)  # Default to 1000

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        
        try:
            service_quotas = session.client("service-quotas", region_name=region)
            
            # Lambda concurrent executions quota code
            quota_code = "L-B99A9384"
            service_code = "lambda"
            
            # Get current quota
            try:
                current_quota = service_quotas.get_service_quota(
                    ServiceCode=service_code,
                    QuotaCode=quota_code
                )
                current_value = current_quota['Quota']['Value']
                
                if current_value >= requested_limit:
                    return jsonify({
                        'success': True,
                        'message': f'Current quota ({current_value}) is already sufficient. No increase needed.',
                        'current_quota': current_value
                    })
                
                # Request quota increase
                logger.info(f"[QUOTA] Requesting increase from {current_value} to {requested_limit}")
                
                # Request quota increase
                try:
                    quota_request = service_quotas.request_service_quota_increase(
                        ServiceCode=service_code,
                        QuotaCode=quota_code,
                        DesiredValue=requested_limit
                    )
                    
                    request_id = quota_request['RequestedQuota']['RequestId']
                    logger.info(f"[QUOTA] ✓ Quota increase requested. Request ID: {request_id}")
                    
                    return jsonify({
                        'success': True,
                        'message': f'Quota increase requested: {current_value} → {requested_limit}',
                        'request_id': request_id,
                        'current_quota': current_value,
                        'requested_quota': requested_limit,
                        'note': 'AWS Support will review and approve (usually within 24 hours)'
                    })
                except service_quotas.exceptions.DependencyAccessDeniedException:
                    return jsonify({
                        'success': False,
                        'error': 'Service Quotas API not available. Request quota increase manually via AWS Support Center → Service Quotas → Lambda → Concurrent executions'
                    }), 403
                except service_quotas.exceptions.QuotaExceededException:
                    return jsonify({
                        'success': False,
                        'error': f'Cannot request {requested_limit}. Maximum allowed is lower. Check AWS Console for limits.'
                    }), 400
                except Exception as e:
                    error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
                    if error_code == 'AccessDenied':
                        return jsonify({
                            'success': False,
                            'error': 'Access denied. Request quota increase manually via AWS Support Center.'
                        }), 403
                    raise
                    
            except service_quotas.exceptions.NoSuchResourceException:
                return jsonify({
                    'success': False,
                    'error': 'Quota not found. This account may not have Service Quotas enabled.'
                }), 404
                
        except Exception as e:
            logger.error(f"Error requesting quota increase: {e}")
            return jsonify({
                'success': False,
                'error': f'Could not request quota increase: {str(e)}. Request manually via AWS Support Center.'
            }), 500
            
    except Exception as e:
        logger.error(f"Error requesting quota increase: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/fix-lambda-concurrency', methods=['POST'])
@login_required
def fix_lambda_concurrency():
    """Remove reserved concurrency limit to allow 1000+ concurrent executions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        lam = session.client("lambda")
        
        # Get configurable lambda prefix for multi-tenant support
        naming_config_fix = get_naming_config()
        lambda_prefix_fix = naming_config_fix['production_lambda_name']

        try:
            # Check current concurrency settings
            concurrency_config = lam.get_function_concurrency(FunctionName=lambda_prefix_fix)
            reserved_concurrency = concurrency_config.get('ReservedConcurrentExecutions')
            
            if reserved_concurrency:
                logger.info(f"[LAMBDA] Current reserved concurrency: {reserved_concurrency}")
                # Delete reserved concurrency to use account limit (1000+)
                lam.delete_function_concurrency(FunctionName=lambda_prefix_fix)
                logger.info(f"[LAMBDA] ✓ Removed reserved concurrency limit ({reserved_concurrency} → account limit)")
                return jsonify({
                    'success': True,
                    'message': f'Removed reserved concurrency limit ({reserved_concurrency}). Lambda can now use account limit (1000+).',
                    'previous_limit': reserved_concurrency,
                    'new_limit': 'Account limit (1000+)'
                })
            else:
                return jsonify({
                    'success': True,
                    'message': 'No reserved concurrency limit found. Lambda is using account limit (1000+).',
                    'current_limit': 'Account limit (1000+)'
                })
        except lam.exceptions.ResourceNotFoundException:
            return jsonify({
                'success': False,
                'error': f'Lambda function {lambda_prefix_fix} not found. Create it first.'
            }), 404
        except Exception as e:
            # Try to delete anyway (might be a different error)
            try:
                lam.delete_function_concurrency(FunctionName=lambda_prefix_fix)
                logger.info(f"[LAMBDA] ✓ Removed reserved concurrency limit")
                return jsonify({
                    'success': True,
                    'message': 'Removed reserved concurrency limit. Lambda can now use account limit (1000+).'
                })
            except Exception as e2:
                logger.error(f"Error fixing concurrency: {e2}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fix concurrency limit: {str(e2)}'
                }), 500

    except Exception as e:
        logger.error(f"Error fixing Lambda concurrency: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

        return [{'email': u['email'], 'success': False, 'error': str(e)} for u in batch_users]
        return [{'email': u['email'], 'success': False, 'error': str(e)} for u in batch_users]

@aws_manager.route('/api/aws/debug-version', methods=['GET'])
def debug_version():
    """Debug endpoint to check code version"""
    import os
    import datetime
    
    file_path = os.path.abspath(__file__)
    mod_time = os.path.getmtime(file_path)
    mod_time_str = datetime.datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d %H:%M:%S')
    
    return jsonify({
        'version': 'REFACTORED_FLAT_EXECUTION_MODEL_V2_LOGGING_ENHANCED',
        'timestamp': time.time(),
        'file_path': file_path,
        'last_modified': mod_time_str,
        'message': 'If you see this, the new code is active.'
    })

@aws_manager.route('/api/aws/bulk-generate', methods=['POST'])
@login_required
def bulk_generate():
    """
    Start background job to generate app passwords in bulk.
    Invokes Lambdas synchronously on the server side and saves results to DB.
    """
    data = request.get_json()
    access_key = data.get('access_key', '').strip()
    secret_key = data.get('secret_key', '').strip()
    region = data.get('region', '').strip()
    users_raw = data.get('users', [])
    users_per_function = data.get('users_per_function', 10)  # Default to 10 if not provided
    
    # Validate users_per_function
    try:
        users_per_function = int(users_per_function)
        if users_per_function < 1 or users_per_function > 50:
            users_per_function = 10  # Reset to default if invalid
            logger.warning(f"[BULK] Invalid users_per_function value ({users_per_function}), using default: 10. Allowed range: 1-50")
    except (ValueError, TypeError):
        users_per_function = 10
        logger.warning(f"[BULK] Invalid users_per_function type, using default: 10")
    
    logger.info(f"[BULK] Users per function setting: {users_per_function}")
    
    if not users_raw:
        return jsonify({'success': False, 'error': 'No users provided'}), 400

    # [MULTI-USER] Get lambda_prefix based on logged-in user
    # Priority: 1) Request data, 2) Session user dynamic prefix, 3) Database config
    _session_user = session.get('user')
    if data.get('lambda_prefix', '').strip():
        lambda_prefix_bulk = data.get('lambda_prefix').strip()
    elif _session_user:
        lambda_prefix_bulk = f"{_session_user.split('@')[0].lower()}-chromium"
    else:
        lambda_prefix_bulk = get_naming_config().get('production_lambda_name', 'gbot-chromium')
    
    dynamodb_table_bulk = data.get('dynamodb_table', '').strip() or get_naming_config().get('dynamodb_table', 'gbot-app-passwords')
    
    # CRITICAL: Pre-compute role_name BEFORE thread starts (while session is available)
    # This avoids "Working outside of request context" error in background thread
    naming_config_precomputed = get_naming_config()
    lambda_role_name_bulk = naming_config_precomputed.get('lambda_role_name', 'gbot-app-password-lambda-role')
    
    logger.info(f"[BULK] Using lambda_prefix: {lambda_prefix_bulk} (user: {_session_user})")
    logger.info(f"[BULK] Using dynamodb_table: {dynamodb_table_bulk}")
    logger.info(f"[BULK] Using lambda_role_name: {lambda_role_name_bulk}")
    
    # Auto-clear DynamoDB before starting new batch
    try:
        session_boto = get_boto3_session(access_key, secret_key, region)
        dynamodb = session_boto.resource('dynamodb')
        table = dynamodb.Table(dynamodb_table_bulk)
        
        # Quick scan and delete old items
        response = table.scan()
        items = response.get('Items', [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={'email': item['email']})
            logger.info(f"[DYNAMODB] ✓ Auto-cleared {len(items)} old items before new batch")
    except Exception as e:
        logger.warning(f"[DYNAMODB] Could not auto-clear (table may not exist): {e}")
        # Continue anyway - not critical

    # Parse users
    users = []
    logger.info(f"[BULK] [DEBUG] Raw users_raw count: {len(users_raw)}")
    logger.info(f"[BULK] [DEBUG] Raw users_raw type: {type(users_raw)}")
    logger.info(f"[BULK] [DEBUG] First 3 raw entries: {users_raw[:3] if len(users_raw) >= 3 else users_raw}")
    
    for u in users_raw:
        parts = u.split(':', 1)
        if len(parts) == 2:
            users.append({'email': parts[0].strip(), 'password': parts[1].strip()})
        else:
            logger.warning(f"[BULK] [DEBUG] Skipped invalid entry (no colon): {u[:50]}...")
    
    if not users:
        return jsonify({'success': False, 'error': 'No valid user:password pairs found'}), 400

    logger.info(f"[BULK-V2-FIXED] [DEBUG] Parsed users count: {len(users)}")
    logger.info(f"[BULK-V2-FIXED] Received {len(users_raw)} raw user entries, parsed {len(users)} valid users")

    # --- PROOF OF LIFE LOGGING ---
    print("\n" + "!"*80, flush=True)
    print("!!! NEW CODE LOADED - V2 FIXED !!!", flush=True)
    print("\n" + "!"*80, flush=True)
    print("!!! NEW CODE LOADED - RESTORED NESTED PARALLELISM !!!", flush=True)
    print(f"!!! TIMESTAMP: {time.time()} !!!", flush=True)
    print("!"*80 + "\n", flush=True)
    # -----------------------------

    job_id = str(int(time.time()))
    with jobs_lock:
        active_jobs[job_id] = {
            'total': len(users),
            'completed': 0,
            'success': 0,
            'failed': 0,
            'results': [],
            'status': 'processing'
        }
        # Save to file for other workers
        save_jobs({job_id: active_jobs[job_id]})
    
    logger.info(f"[BULK] Created job {job_id} for {len(users)} users")

    # Start background thread
    # We pass app_context explicitly if needed, but db operations need app context inside the thread
    from app import app
    
    def background_process(app, job_id, users, access_key, secret_key, region, lambda_prefix_bulk, lambda_role_name_bulk, dynamodb_table_bulk, users_per_function_param):
        """Background process to handle bulk user processing across geos"""
        # --- PROOF OF LIFE LOGGING ---
        print("\n" + "!"*80, flush=True)
        print(f"!!! BACKGROUND PROCESS STARTED - NESTED PARALLELISM - Job {job_id} !!!", flush=True)
        print("!"*80 + "\n", flush=True)
        # -----------------------------

        logger.info("=" * 80)
        logger.info(f"[BULK] ========== BACKGROUND PROCESS STARTED ==========")
        logger.info(f"[BULK] Job ID: {job_id}")
        logger.info(f"[BULK] Total users: {len(users)}")
        logger.info(f"[BULK] Region: {region}")
        logger.info(f"[BULK] Thread ID: {threading.current_thread().ident}")
        logger.info(f"[BULK] Thread Name: {threading.current_thread().name}")
        logger.info("=" * 80)
        
        # Force flush logger
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        
        # Ensure job exists before starting processing
        with jobs_lock:
            if job_id not in active_jobs:
                logger.error(f"[BULK] Job {job_id} not found in active_jobs at start of background_process!")
                # Try to recreate it
                active_jobs[job_id] = {
                    'total': len(users),
                    'completed': 0,
                    'success': 0,
                    'failed': 0,
                    'results': [],
                    'status': 'processing'
                }
                logger.info(f"[BULK] Recreated job {job_id}")
            else:
                logger.info(f"[BULK] Job {job_id} found in active_jobs")
        
        try:
            logger.info("=" * 80)
            logger.info(f"[BULK] ===== ENTERING APP CONTEXT =====")
            logger.info(f"[BULK] About to enter app context...")
            logger.info("=" * 80)
            print(f"[APP_CONTEXT] About to enter app context for job {job_id}", flush=True)
            
            with app.app_context():
                logger.info("=" * 80)
                logger.info(f"[BULK] ===== APP CONTEXT ENTERED SUCCESSFULLY =====")
                logger.info(f"[BULK] App context is active")
                logger.info("=" * 80)
                print(f"[APP_CONTEXT] App context entered successfully for job {job_id}", flush=True)
                import sys
                sys.stdout.flush()
                sys.stderr.flush()
                # Pre-detect Lambda functions across ALL geos
                # This is necessary because functions are distributed across multiple AWS regions
                lambda_functions = []
                # We no longer pre-detect Lambda functions across all regions
                # Instead, we'll look for functions in their assigned regions during processing
                logger.info(f"[BULK] Will process users using geo-distributed Lambda functions")
                
                # CRITICAL FIX: Use ALL existing Lambda functions (not calculate based on user count)
                # Discover which geos actually have Lambda functions
                logger.info("=" * 60)
                logger.info(f"[BULK] Discovering ALL existing Lambda functions across all regions...")
                logger.info("=" * 60)
                
                geos_with_functions = {}  # {geo: [list of function_names]}
                
                # Check ALL available regions for Lambda functions
                # This ensures we find Lambdas in any geo they were created in
                # (e.g., af-south-1, us-east-1, eu-west-1, etc.)
                for geo in AVAILABLE_GEO_REGIONS:
                    try:
                        logger.info(f"[BULK] Checking geo {geo} for Lambda functions...")
                        geo_session = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=geo
                        )
                        geo_lam = geo_session.client("lambda")
                        geo_functions = geo_lam.list_functions()
                        
                        matching_functions = [
                            fn['FunctionName'] for fn in geo_functions.get('Functions', [])
                            if fn['FunctionName'].startswith(lambda_prefix_bulk)
                        ]
                        
                        if matching_functions:
                            geos_with_functions[geo] = matching_functions
                            logger.info(f"[BULK] ✓ Geo {geo} has {len(matching_functions)} Lambda function(s): {matching_functions}")
                        else:
                            logger.info(f"[BULK] ✗ Geo {geo} has no matching Lambda functions")
                    except Exception as geo_check_err:
                        logger.warning(f"[BULK] Could not check geo {geo}: {geo_check_err}")
                        continue
                
                if not geos_with_functions:
                    error_msg = f"No Lambda functions found in any geo! Please create Lambda functions first."
                    logger.error(f"[BULK] ❌❌❌ {error_msg}")
                    raise Exception(error_msg)
                
                # Count total existing lambdas
                total_existing_lambdas = sum(len(funcs) for funcs in geos_with_functions.values())
                
                logger.info("=" * 60)
                logger.info(f"[BULK] Found {len(geos_with_functions)} geo(s) with {total_existing_lambdas} total Lambda function(s):")
                for geo, funcs in geos_with_functions.items():
                    logger.info(f"[BULK]   - {geo}: {len(funcs)} function(s) - {', '.join(funcs)}")
                logger.info("=" * 60)
                
                # Use ALL existing lambdas - create flat list of (geo, function_name) tuples
                all_lambdas_flat = []
                for geo, func_names in geos_with_functions.items():
                    for func_name in func_names:
                        all_lambdas_flat.append((geo, func_name))
                
                if not all_lambdas_flat:
                    error_msg = f"No Lambda functions found to process users! Please create Lambda functions first."
                    logger.error(f"[BULK] ❌❌❌ {error_msg}")
                    raise Exception(error_msg)
                
                total_users = len(users)
                USERS_PER_FUNCTION = users_per_function_param  # Uses explicit parameter from caller
                
                # Distribute users across ALL existing lambdas
                # Each lambda gets up to 10 users, distributed round-robin
                user_batches = []  # List of [geo, function_name, user_batch] lists
                
                logger.info(f"[BULK] Distributing {total_users} users across {total_existing_lambdas} Lambda function(s)")
                logger.info(f"[BULK] Each Lambda will process up to {USERS_PER_FUNCTION} user(s)")
                logger.info(f"[BULK] All lambdas flat list: {all_lambdas_flat[:5]}... (showing first 5)")
                
                # SEQUENTIAL FILL: Fill each Lambda up to USERS_PER_FUNCTION before moving to next
                # This ensures each Lambda gets the full batch size before using another Lambda
                logger.info(f"[BULK] Using SEQUENTIAL FILL distribution (up to {USERS_PER_FUNCTION} users per Lambda)")
                
                current_lambda_idx = 0
                current_batch_users = []
                
                for user_idx, user in enumerate(users):
                    current_batch_users.append(user)
                    
                    # If we've reached the limit for this Lambda, or this is the last user
                    if len(current_batch_users) >= USERS_PER_FUNCTION or user_idx == len(users) - 1:
                        # Get the Lambda for this batch
                        geo, function_name = all_lambdas_flat[current_lambda_idx % len(all_lambdas_flat)]
                        
                        # Add the batch
                        user_batches.append([geo, function_name, current_batch_users.copy()])
                        logger.info(f"[BULK] Created batch for {function_name}: {len(current_batch_users)} users")
                        
                        # Move to next Lambda and reset batch
                        current_lambda_idx += 1
                        current_batch_users = []
                
                # Log batch summary
                total_users_in_batches = sum(len(batch[2]) for batch in user_batches)
                logger.info(f"[BULK] [DEBUG] Total batches created: {len(user_batches)}")
                logger.info(f"[BULK] [DEBUG] Total users in all batches: {total_users_in_batches}")
                for i, batch in enumerate(user_batches):
                    logger.info(f"[BULK] [DEBUG] Batch {i+1}: {batch[1]} has {len(batch[2])} users")
                if not user_batches:
                    error_msg = f"Failed to create user batches! No batches created for {total_users} users."
                    logger.error(f"[BULK] ❌❌❌ {error_msg}")
                    raise Exception(error_msg)
                
                logger.info("=" * 60)
                logger.info(f"[BULK] Created {len(user_batches)} batch(es) across {len(geos_with_functions)} geo(s):")
                for geo, func_name, batch_users in user_batches:
                    logger.info(f"[BULK]   - {geo}/{func_name}: {len(batch_users)} user(s)")
                logger.info("=" * 60)
                
                # BATCH PROCESSING: Process based on users_per_function (dynamic)
                # We set a high safety limit here to allow user configuration (e.g. 20)
                
                def process_user_batch_sync(user_batch, assigned_function_name, lambda_region=None):
                    """
                    Process a batch of users synchronously (wait for completion).
                    Returns list of results, one per user.
                    This is used for sequential processing within each geo.
                
                    Args:
                        user_batch: List of user dicts to process
                        assigned_function_name: Name of Lambda function to invoke
                        lambda_region: AWS region where Lambda function is deployed (defaults to 'region' variable)
                    """
                    with app.app_context():
                        # CRITICAL: Enforce higher safety limit (allow up to 50)
                        MAX_USERS_PER_BATCH = 50
                        if len(user_batch) > MAX_USERS_PER_BATCH:
                            logger.error(f"[BULK] [{assigned_function_name}] ⚠️ CRITICAL ERROR: Batch has {len(user_batch)} users, exceeding safety limit of {MAX_USERS_PER_BATCH}!")
                            logger.error(f"[BULK] [{assigned_function_name}] Truncating batch to {MAX_USERS_PER_BATCH} users")
                            user_batch = user_batch[:MAX_USERS_PER_BATCH]
                        
                        # Use lambda_region if provided, otherwise fall back to user's selected region
                        target_region = lambda_region if lambda_region else region
                        
                        # Create INDEPENDENT boto3 session and clients for this batch
                        session_batch = boto3.Session(
                            aws_access_key_id=access_key,
                            aws_secret_access_key=secret_key,
                            region_name=target_region
                        )
                        
                        # Each batch gets its own Lambda client with extended timeout
                        # CRITICAL: Set read_timeout to 1000 seconds (16+ minutes) to handle batch processing
                        # Lambda timeout is 900 seconds, so we need client timeout > Lambda timeout
                        # IMPORTANT: Lambda client uses the region from session_batch (target_region)
                        lam_batch = session_batch.client("lambda", config=Config(
                            max_pool_connections=10,
                            retries={'max_attempts': 0},
                            read_timeout=1000,  # 16+ minutes - must exceed Lambda timeout (900s)
                            connect_timeout=60  # 60 seconds connection timeout
                        ))
                        
                        # Each batch gets its own DynamoDB resource
                        dynamodb_batch = session_batch.resource('dynamodb', config=Config(
                            max_pool_connections=10
                        ))
                        # Use configurable DynamoDB table name
                        dynamodb_table_batch = dynamodb_table_bulk  # From outer scope
                        table_batch = dynamodb_batch.Table(dynamodb_table_batch)
                        logger.info(f"[BULK] Using DynamoDB table: {dynamodb_table_batch}")
                    
                        # Prepare all users for processing - NO pre-filtering
                        # Lambda will handle deduplication if needed
                        batch_results = []
                        users_to_process = user_batch  # Process ALL users in the batch (already limited to 10)
                    
                        # Final validation before sending to Lambda
                        if len(users_to_process) > MAX_USERS_PER_BATCH:
                            logger.error(f"[BULK] [{assigned_function_name}] ⚠️ FINAL CHECK FAILED: {len(users_to_process)} users exceeds {MAX_USERS_PER_BATCH} limit!")
                            users_to_process = users_to_process[:MAX_USERS_PER_BATCH]
                        
                        logger.info(f"[BULK] [{assigned_function_name}] Will process {len(users_to_process)} user(s) in batch (MAX: {MAX_USERS_PER_BATCH})")
                    
                        # Mark emails as being processed (for duplicate detection across parallel geos)
                        for user in users_to_process:
                            email = user['email']
                            with processing_lock:
                                if email in processing_emails:
                                    logger.warning(f"[BULK] ⚠️ WARNING: {email} is already being processed in another geo!")
                                processing_emails.add(email)
                    
                        # Prepare batch payload for Lambda
                        # Safety limit - should match the users_per_function validation limit
                        MAX_USERS_PER_BATCH = 50
                        if len(users_to_process) > MAX_USERS_PER_BATCH:
                            logger.warning(f"[BULK] [{assigned_function_name}] ⚠️ PAYLOAD CHECK: Truncating {len(users_to_process)} users to {MAX_USERS_PER_BATCH}")
                            users_to_process = users_to_process[:MAX_USERS_PER_BATCH]
                        
                        batch_payload = {
                            "users": [
                                {"email": u['email'], "password": u['password']}
                                for u in users_to_process
                            ]
                        }
                        
                        logger.info("=" * 60)
                        logger.info(f"[BULK] [{assigned_function_name}] PREPARING TO INVOKE LAMBDA")
                        logger.info(f"[BULK] [{assigned_function_name}] Batch size: {len(users_to_process)} user(s) (MAX: {MAX_USERS_PER_BATCH})")
                        if len(users_to_process) > MAX_USERS_PER_BATCH:
                            logger.error(f"[BULK] [{assigned_function_name}] ⚠️ ERROR: Batch size {len(users_to_process)} exceeds limit {MAX_USERS_PER_BATCH}!")
                        logger.info(f"[BULK] [{assigned_function_name}] Users in batch: {[u['email'] for u in users_to_process]}")
                        logger.info(f"[BULK] [{assigned_function_name}] Payload structure: {{'users': [{{'email': ..., 'password': ...}}]}}")
                        logger.info(f"[BULK] [{assigned_function_name}] Payload JSON length: {len(json.dumps(batch_payload))} bytes")
                        logger.info(f"[BULK] [{assigned_function_name}] Payload preview: {json.dumps(batch_payload)[:500]}...")
                        logger.info("=" * 60)
                        
                        # Rate limiting: Acquire semaphore to limit concurrent invocations
                        # NOTE: Semaphore limit is now 500 to allow all functions in all geos to start in parallel
                        # The semaphore is held for the duration of the Lambda invocation (up to 15 minutes)
                        # This ensures we don't exceed AWS account limits while allowing maximum parallelism
                        logger.info(f"[BULK] [{assigned_function_name}] Acquiring semaphore for Lambda invocation...")
                        lambda_invocation_semaphore.acquire()
                        logger.info(f"[BULK] [{assigned_function_name}] ✓ Semaphore acquired, invoking Lambda NOW (parallel execution enabled)")
                        
                        # CRITICAL: Log all invocation details before attempting
                        logger.info("=" * 60)
                        logger.info(f"[BULK] [{assigned_function_name}] ===== LAMBDA INVOCATION DETAILS =====")
                        logger.info(f"[BULK] [{assigned_function_name}] Function Name: {assigned_function_name}")
                        logger.info(f"[BULK] [{assigned_function_name}] Target Region: {target_region}")
                        logger.info(f"[BULK] [{assigned_function_name}] Batch Size: {len(users_to_process)} user(s)")
                        logger.info(f"[BULK] [{assigned_function_name}] Invocation Type: RequestResponse (SYNC)")
                        logger.info(f"[BULK] [{assigned_function_name}] Payload Size: {len(json.dumps(batch_payload))} bytes")
                        logger.info("=" * 60)
                        
                        try:
                            # Retry logic for rate limiting
                            max_retries = 3
                            resp = None
                            for attempt in range(max_retries):
                                try:
                                    logger.info(f"[BULK] [{assigned_function_name}] Attempt {attempt + 1}/{max_retries}: Invoking Lambda...")
                                    logger.info(f"[BULK] [{assigned_function_name}] FunctionName: {assigned_function_name}")
                                    logger.info(f"[BULK] [{assigned_function_name}] Region: {target_region}")
                                    
                                    # Use SYNC invocation to wait for completion (sequential processing)
                                    resp = lam_batch.invoke(
                                        FunctionName=assigned_function_name,
                                        InvocationType="RequestResponse",  # SYNC - wait for completion
                                        Payload=json.dumps(batch_payload).encode("utf-8"),
                                    )
                                    
                                    logger.info(f"[BULK] [{assigned_function_name}] ✓ Lambda invoke() call completed (no exception)")
                                    logger.info(f"[BULK] [{assigned_function_name}] Response object received: {type(resp)}")
                                    logger.info(f"[BULK] [{assigned_function_name}] StatusCode: {resp.get('StatusCode')}")
                                    logger.info(f"[BULK] [{assigned_function_name}] FunctionError: {resp.get('FunctionError')}")
                                
                                    # Parse Lambda response
                                    payload = resp.get("Payload")
                                    body = payload.read().decode("utf-8") if payload else "{}"
                                    logger.info("=" * 60)
                                    logger.info(f"[BULK] [{assigned_function_name}] LAMBDA RESPONSE RECEIVED")
                                    logger.info(f"[BULK] [{assigned_function_name}] Response status code: {resp.get('StatusCode')}")
                                    logger.info(f"[BULK] [{assigned_function_name}] Response body (first 2000 chars): {body[:2000]}")
                                    logger.info("=" * 60)
                                
                                    try:
                                        lambda_response = json.loads(body)
                                    except json.JSONDecodeError as je:
                                        logger.error(f"[BULK] Failed to parse Lambda response as JSON: {je}")
                                        # All users in batch fail
                                        for u in users_to_process:
                                            batch_results.append({
                                                'email': u['email'],
                                                'success': False,
                                                'error': f'Invalid JSON response: {body[:200]}'
                                            })
                                        return batch_results
                                
                                    # Handle batch response format
                                    if lambda_response.get("status") == "completed" and "results" in lambda_response:
                                        # Batch processing response
                                        lambda_results = lambda_response.get("results", [])
                                        logger.info(f"[BULK] [{assigned_function_name}] Lambda returned {len(lambda_results)} results for {len(users_to_process)} users sent")
                                        for lambda_result in lambda_results:
                                            email = lambda_result.get("email", "unknown")
                                            lambda_status = lambda_result.get("status", "unknown")
                                            app_password = lambda_result.get("app_password")
                                            error_msg = lambda_result.get("error_message", "Unknown error")
                                        
                                            if lambda_status == 'success' and app_password:
                                                logger.info(f"[BULK] Saving password for {email} to DB")
                                                try:
                                                    save_app_password(email, app_password)
                                                    logger.info(f"[BULK] ✓ Successfully processed {email}")
                                                except Exception as db_err:
                                                    logger.error(f"[BULK] Failed to save to DB for {email}: {db_err}")
                                                batch_results.append({
                                                    'email': email,
                                                    'success': True,
                                                    'app_password': app_password
                                                })
                                            else:
                                                logger.warning(f"[BULK] ✗ Lambda failed for {email}: {error_msg}")
                                                batch_results.append({
                                                    'email': email,
                                                    'success': False,
                                                    'error': error_msg
                                                })
                                        break  # Success, exit retry loop
                                    else:
                                        # Fallback: single user response format (backward compatibility)
                                        lambda_status = lambda_response.get('status', 'unknown')
                                        app_password = lambda_response.get('app_password')
                                        error_msg = lambda_response.get('error_message', 'Unknown error')
                                    
                                        # If only one user in batch, use single response format
                                        if len(users_to_process) == 1:
                                            email = users_to_process[0]['email']
                                            if lambda_status == 'success' and app_password:
                                                try:
                                                    save_app_password(email, app_password)
                                                    logger.info(f"[BULK] ✓ Successfully processed {email}")
                                                except Exception as db_err:
                                                    logger.error(f"[BULK] Failed to save to DB for {email}: {db_err}")
                                                batch_results.append({
                                                    'email': email,
                                                    'success': True,
                                                    'app_password': app_password
                                                })
                                            else:
                                                batch_results.append({
                                                    'email': email,
                                                    'success': False,
                                                    'error': error_msg
                                                })
                                            break  # Success, exit retry loop
                                        else:
                                            # Multiple users but got single response - all fail
                                            logger.error(f"[BULK] Expected batch response but got single user format")
                                            for u in users_to_process:
                                                batch_results.append({
                                                    'email': u['email'],
                                                    'success': False,
                                                    'error': 'Invalid response format from Lambda'
                                                })
                                            return batch_results
                                    
                                except ClientError as ce:
                                    error_code = ce.response['Error']['Code']
                                    error_message = ce.response['Error'].get('Message', '')
                                    
                                    if error_code == 'ResourceNotFoundException':
                                        logger.error(f"[BULK] Lambda function {assigned_function_name} not found")
                                        # Try to fall back to default function
                                        if assigned_function_name != PRODUCTION_LAMBDA_NAME:
                                            logger.warning(f"[BULK] Falling back to default function {PRODUCTION_LAMBDA_NAME}")
                                            assigned_function_name = PRODUCTION_LAMBDA_NAME
                                            continue  # Retry with default function
                                        else:
                                            # All users in batch fail
                                            for u in users_to_process:
                                                batch_results.append({
                                                    'email': u['email'],
                                                    'success': False,
                                                    'error': f'Lambda function {assigned_function_name} not found'
                                                })
                                            return batch_results
                                    
                                    if error_code == 'TooManyRequestsException' or error_code == 'ThrottlingException':
                                        if attempt < max_retries - 1:
                                            base_wait = (2 ** attempt) * 2
                                            jitter = random.uniform(0, 1)
                                            wait_time = base_wait + jitter
                                            logger.warning(f"[BULK] Rate limited for batch, retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
                                            time.sleep(wait_time)
                                        else:
                                            # All users in batch fail
                                            for u in users_to_process:
                                                batch_results.append({
                                                    'email': u['email'],
                                                    'success': False,
                                                    'error': f'Rate limited: {error_message}'
                                                })
                                            return batch_results
                                    else:
                                        logger.error(f"[BULK] AWS error: {error_code} - {error_message}")
                                        # All users in batch fail
                                        for u in users_to_process:
                                            batch_results.append({
                                                'email': u['email'],
                                                'success': False,
                                                'error': f'AWS Error ({error_code}): {error_message}'
                                            })
                                        return batch_results
                                except Exception as invoke_err:
                                    # Check if it's a timeout error
                                    if 'Read timeout' in str(invoke_err) or 'timeout' in str(invoke_err).lower():
                                        logger.error(f"[BULK] Read timeout on attempt {attempt + 1} - Lambda may still be processing")
                                        if attempt == max_retries - 1:
                                            # Final attempt failed - mark as timeout
                                            for u in users_to_process:
                                                batch_results.append({
                                                    'email': u['email'],
                                                    'success': False,
                                                    'error': f'Read timeout - Lambda processing may have exceeded timeout'
                                                })
                                            return batch_results
                                        time.sleep(5)
                                        continue
                                    else:
                                        logger.error(f"[BULK] Invocation error: {invoke_err}")
                                        if attempt == max_retries - 1:
                                            # Final attempt failed
                                            for u in users_to_process:
                                                batch_results.append({
                                                    'email': u['email'],
                                                    'success': False,
                                                    'error': f'Invocation error: {str(invoke_err)}'
                                                })
                                            return batch_results
                                        time.sleep(2)
                                        continue
                        finally:
                            lambda_invocation_semaphore.release()
                        
                        # Remove processed emails from tracking set
                        for u in users_to_process:
                            with processing_lock:
                                processing_emails.discard(u['email'])
                    
                        return batch_results
                        
                # Group batches by geo for parallel processing within each geo
                batches_by_geo = {}  # {geo: [(function_name, user_batch), ...]}
                logger.info(f"[BULK] Grouping {len(user_batches)} batches by geo...")
                for geo, function_name, batch_users in user_batches:
                    logger.info(f"[BULK] Processing batch: geo={geo}, function={function_name}, batch_size={len(batch_users)}, users={[u['email'] for u in batch_users[:3]]}{'...' if len(batch_users) > 3 else ''}")
                    if geo not in batches_by_geo:
                        batches_by_geo[geo] = []
                    batches_by_geo[geo].append((function_name, batch_users))
                    logger.info(f"[BULK] Added to geo {geo}: Function {function_name} with {len(batch_users)} user(s)")
            
                logger.info("=" * 60)
                logger.info(f"[BULK] Batches per geo:")
                for geo, geo_batches in sorted(batches_by_geo.items()):
                    total_users_in_geo = sum(len(batch) for _, batch in geo_batches)
                    logger.info(f"[BULK]   - {geo}: {len(geo_batches)} function(s), {total_users_in_geo} user(s)")
                logger.info(f"[BULK] TOTAL GEOS TO PROCESS: {len(batches_by_geo)}")
                logger.info(f"[BULK] Geo list: {list(batches_by_geo.keys())}")
                logger.info("=" * 60)
                
                if not batches_by_geo:
                    error_msg = f"No batches created for any geo! Cannot process users."
                    logger.error(f"[BULK] ❌❌❌ {error_msg}")
                    raise Exception(error_msg)
            
                def process_geo_parallel(geo, geo_batches_list):
                    """
                    Process all batches in a geo in PARALLEL (multiple functions at the same time).
                    Uses ALL existing Lambda functions in the geo.
                    Maximum 10 functions per geo at the same time (AWS Lambda concurrency limit).
                    Minimum 2 functions per geo at the same time (as requested).
                    """
                    try:
                        logger.info("=" * 60)
                        logger.info(f"[BULK] [{geo}] ===== STARTING PARALLEL PROCESSING =====")
                        logger.info(f"[BULK] [{geo}] Total functions to process: {len(geo_batches_list)}")
                        logger.info(f"[BULK] [{geo}] Function names: {[func_name for func_name, _ in geo_batches_list]}")
                        
                        # Calculate max workers: min(10, number of functions, but at least 2 if we have 2+ functions)
                        max_workers = min(10, len(geo_batches_list))
                        if len(geo_batches_list) >= 2 and max_workers < 2:
                            max_workers = 2
                        logger.info(f"[BULK] [{geo}] Will process {max_workers} function(s) in parallel (max 10 per geo)")
                        logger.info("=" * 60)
                        
                        # Create boto3 session for this geo (use the geo's region)
                        try:
                            logger.info(f"[BULK] [{geo}] Creating boto3 session for region: {geo}")
                            session_boto = boto3.Session(
                                aws_access_key_id=access_key,
                                aws_secret_access_key=secret_key,
                                region_name=geo  # Use geo as the region
                            )
                            
                            # Verify credentials work for this region
                            try:
                                sts = session_boto.client('sts')
                                identity = sts.get_caller_identity()
                                logger.info(f"[BULK] [{geo}] ✓ Credentials verified. Account: {identity.get('Account')}")
                            except Exception as sts_err:
                                logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL: Credential verification failed: {sts_err}")
                                logger.error(traceback.format_exc())
                                raise Exception(f"Credential verification failed for {geo}: {sts_err}")
                            
                            lam_client = session_boto.client("lambda", config=Config(
                                max_pool_connections=10,
                                retries={'max_attempts': 3}
                            ))
                            
                            logger.info(f"[BULK] [{geo}] Listing Lambda functions in region {geo}...")
                            all_functions = lam_client.list_functions()
                            existing_function_names = [fn['FunctionName'] for fn in all_functions.get('Functions', [])]
                            logger.info(f"[BULK] [{geo}] ✓ Found {len(existing_function_names)} existing function(s) in {geo}: {existing_function_names[:5]}{'...' if len(existing_function_names) > 5 else ''}")
                        except Exception as e:
                            logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL ERROR: Could not initialize session or list functions: {e}")
                            logger.error(traceback.format_exc())
                            # Don't return empty - raise exception so it's caught by outer handler
                            raise Exception(f"Failed to initialize {geo}: {e}")
                        
                        # Helper function to process a single function
                        def process_single_function(func_name, batch_users, batch_idx):
                            """Process a single Lambda function (thread-safe)"""
                            function_results = []
                            
                            try:
                                logger.info("=" * 60)
                                logger.info(f"[BULK] [{geo}] ===== FUNCTION {batch_idx + 1}/{len(geo_batches_list)} (PARALLEL) =====")
                                logger.info(f"[BULK] [{geo}] Function name: {func_name}")
                                logger.info(f"[BULK] [{geo}] Users in batch: {len(batch_users)}")
                                logger.info(f"[BULK] [{geo}] User emails: {[u['email'] for u in batch_users[:5]]}{'...' if len(batch_users) > 5 else ''}")
                                logger.info("=" * 60)
                            
                                logger.info(f"[BULK] [{geo}] Looking for function: {func_name}")
                                logger.info(f"[BULK] [{geo}] Available functions in {geo}: {existing_function_names}")
                            
                                # Verify function exists (thread-safe check)
                                with threading.Lock():
                                    if func_name not in existing_function_names:
                                        # Try to find any function matching the pattern for this geo
                                        # Function not found - this is an error, function should exist
                                        logger.error(f"[BULK] [{geo}] ✗ Function {func_name} not found in region {geo}!")
                                        logger.error(f"[BULK] [{geo}] Available functions: {existing_function_names}")
                                        logger.error(f"[BULK] [{geo}] This function should have been discovered during Lambda discovery step.")
                                        raise Exception(f"Function {func_name} not found in region {geo}. Please recreate Lambdas.")
                                    else:
                                        logger.info(f"[BULK] [{geo}] ✓ Function {func_name} found in region {geo}")
                                        try:
                                            # Use pre-computed role_name (computed before thread started) to avoid session access
                                            role_arn = ensure_lambda_role(session_boto, role_name=lambda_role_name_bulk)
                                            # Use configurable DynamoDB table name from bulk_generate scope
                                            chromium_env = {
                                                "DYNAMODB_TABLE_NAME": dynamodb_table_bulk,  # Use configurable table name
                                                "DYNAMODB_REGION": "eu-west-1",
                                                "APP_PASSWORDS_S3_BUCKET": S3_BUCKET_NAME,
                                                "APP_PASSWORDS_S3_KEY": "app-passwords.txt",
                                            }
                                            logger.info(f"[BULK] [{geo}] Using DynamoDB table: {dynamodb_table_bulk}")
                                            
                                            # Add proxy configuration if enabled
                                            proxy_config = get_proxy_config()
                                            if proxy_config and proxy_config.get('enabled'):
                                                proxies = parse_proxy_list(proxy_config.get('proxies', ''))
                                                if proxies:
                                                    chromium_env['PROXY_ENABLED'] = 'true'
                                                    chromium_env['PROXY_LIST'] = proxy_config.get('proxies', '')
                                                    logger.info(f"[PROXY] [{geo}] Proxy feature enabled with {len(proxies)} proxy/proxies")
                                                else:
                                                    chromium_env['PROXY_ENABLED'] = 'false'
                                            else:
                                                chromium_env['PROXY_ENABLED'] = 'false'
                                            
                                            # Add 2Captcha configuration if enabled
                                            twocaptcha_config = get_twocaptcha_config()
                                            logger.info(f"[2CAPTCHA] [{geo}] Retrieved config: enabled={twocaptcha_config.get('enabled') if twocaptcha_config else False}, has_api_key={bool(twocaptcha_config and twocaptcha_config.get('api_key'))}")
                                            
                                            if twocaptcha_config and twocaptcha_config.get('enabled') and twocaptcha_config.get('api_key'):
                                                chromium_env['TWOCAPTCHA_ENABLED'] = 'true'
                                                chromium_env['TWOCAPTCHA_API_KEY'] = twocaptcha_config.get('api_key', '')
                                                logger.info(f"[2CAPTCHA] [{geo}] ✓ 2Captcha feature ENABLED for automatic CAPTCHA solving")
                                                logger.info(f"[2CAPTCHA] [{geo}] API key length: {len(chromium_env['TWOCAPTCHA_API_KEY'])} characters")
                                            else:
                                                chromium_env['TWOCAPTCHA_ENABLED'] = 'false'
                                                chromium_env['TWOCAPTCHA_API_KEY'] = ''
                                                if not twocaptcha_config:
                                                    logger.warning(f"[2CAPTCHA] [{geo}] ✗ 2Captcha config not found in database")
                                                elif not twocaptcha_config.get('enabled'):
                                                    logger.warning(f"[2CAPTCHA] [{geo}] ✗ 2Captcha is disabled in database")
                                                elif not twocaptcha_config.get('api_key'):
                                                    logger.warning(f"[2CAPTCHA] [{geo}] ✗ 2Captcha API key is empty in database")
                                                logger.info(f"[2CAPTCHA] [{geo}] 2Captcha feature disabled - Lambda will not solve CAPTCHAs")
                                        
                                            # Extract ECR URI
                                            ecr_uri = None
                                            try:
                                                if existing_function_names:
                                                    existing_func = lam_client.get_function(FunctionName=existing_function_names[0])
                                                    code_location = existing_func.get('Code', {}).get('ImageUri')
                                                    if code_location:
                                                        ecr_uri = code_location
                                            except Exception:
                                                pass
                                        
                                            if not ecr_uri:
                                                sts = session_boto.client('sts')
                                                account_id = sts.get_caller_identity()['Account']
                                                ecr_uri = f"{account_id}.dkr.ecr.{geo}.amazonaws.com/{ECR_REPO_NAME}:{ECR_IMAGE_TAG}"
                                        
                                            if func_name != lambda_prefix_bulk:
                                                create_or_update_lambda(
                                                    session=session_boto,
                                                    function_name=func_name,
                                                    role_arn=role_arn,
                                                    timeout=900,
                                                    env_vars=chromium_env,
                                                    package_type="Image",
                                                    image_uri=ecr_uri,
                                                )
                                                logger.info(f"[BULK] [{geo}] ✓ Created Lambda function: {func_name}")
                                                existing_function_names.append(func_name)
                                        except Exception as create_err:
                                            logger.error(f"[BULK] [{geo}] Failed to create {func_name}: {create_err}")
                                            func_name = lambda_prefix_bulk
                                
                                # Verify function exists
                                try:
                                    all_functions_refresh = lam_client.list_functions()
                                    existing_function_names_refresh = [fn['FunctionName'] for fn in all_functions_refresh.get('Functions', [])]
                                    
                                    if func_name not in existing_function_names_refresh:
                                        logger.error(f"[BULK] [{geo}] ✗ Function {func_name} NOT FOUND in region {geo}!")
                                        for u in batch_users:
                                            function_results.append({
                                                'email': u['email'],
                                                'success': False,
                                                'error': f'Lambda function {func_name} not found in region {geo}'
                                            })
                                        return function_results
                                    
                                    logger.info(f"[BULK] [{geo}] ✓ Verified function exists: {func_name}")
                                except Exception as check_err:
                                    logger.warning(f"[BULK] [{geo}] Could not verify function existence: {check_err}, proceeding anyway...")
                                
                                # Invoke Lambda function
                                logger.info("=" * 60)
                                logger.info(f"[BULK] [{geo}] ===== INVOKING LAMBDA FUNCTION =====")
                                logger.info(f"[BULK] [{geo}] Function name: {func_name}")
                                logger.info(f"[BULK] [{geo}] Region: {geo}")
                                logger.info(f"[BULK] [{geo}] Batch size: {len(batch_users)} user(s)")
                                logger.info(f"[BULK] [{geo}] User emails: {[u['email'] for u in batch_users]}")
                                logger.info("=" * 60)
                                
                                try:
                                    batch_results = process_user_batch_sync(batch_users, func_name, lambda_region=geo)
                                    logger.info(f"[BULK] [{geo}] ✓ Function {func_name} invocation completed")
                                    logger.info(f"[BULK] [{geo}] Results: {sum(1 for r in batch_results if r['success'])}/{len(batch_results)} success")
                                    function_results.extend(batch_results)
                                except Exception as invoke_exception:
                                    logger.error("=" * 60)
                                    logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL ERROR: Lambda invocation failed!")
                                    logger.error(f"[BULK] [{geo}] Function: {func_name}")
                                    logger.error(f"[BULK] [{geo}] Error: {invoke_exception}")
                                    logger.error(f"[BULK] [{geo}] Error type: {type(invoke_exception).__name__}")
                                    logger.error(traceback.format_exc())
                                    logger.error("=" * 60)
                                    # Mark all users as failed
                                    for u in batch_users:
                                        function_results.append({
                                            'email': u['email'],
                                            'success': False,
                                            'error': f'Lambda invocation failed: {str(invoke_exception)}'
                                        })
                                
                            except Exception as func_err:
                                logger.error("=" * 60)
                                logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL ERROR: Function {func_name} failed!")
                                logger.error(f"[BULK] [{geo}] Error: {func_err}")
                                logger.error(f"[BULK] [{geo}] Error type: {type(func_err).__name__}")
                                logger.error(traceback.format_exc())
                                logger.error("=" * 60)
                                for u in batch_users:
                                    function_results.append({
                                        'email': u['email'],
                                        'success': False,
                                        'error': f'Function processing failed: {str(func_err)}'
                                    })
                            
                            return function_results
                        
                        # Process all functions in PARALLEL using ThreadPoolExecutor
                        geo_results = []
                        with ThreadPoolExecutor(max_workers=max_workers) as function_pool:
                            # Submit ALL functions for parallel processing - CRITICAL: Ensure every function is submitted
                            function_futures = {}
                            submitted_function_names = []
                            for batch_idx, (func_name, batch_users) in enumerate(geo_batches_list):
                                try:
                                    future = function_pool.submit(process_single_function, func_name, batch_users, batch_idx)
                                    function_futures[future] = (func_name, batch_idx)
                                    submitted_function_names.append(func_name)
                                    logger.info(f"[BULK] [{geo}] ✓✓✓ Submitted function {func_name} (batch {batch_idx + 1}/{len(geo_batches_list)}) with {len(batch_users)} user(s) for parallel processing")
                                except Exception as submit_func_err:
                                    logger.error(f"[BULK] [{geo}] ✗✗✗ FAILED to submit function {func_name}: {submit_func_err}")
                                    logger.error(traceback.format_exc())
                                    # Add failed results for this batch
                                    for u in batch_users:
                                        geo_results.append({
                                            'email': u.get('email', 'unknown'),
                                            'success': False,
                                            'error': f'Failed to submit function {func_name} to thread pool: {str(submit_func_err)}'
                                        })
                            
                            # Verify ALL functions were submitted
                            expected_functions = set(func_name for func_name, _ in geo_batches_list)
                            submitted_functions = set(submitted_function_names)
                            missing_functions = expected_functions - submitted_functions
                            if missing_functions:
                                logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL: {len(missing_functions)} function(s) were NOT submitted: {missing_functions}")
                                logger.error(f"[BULK] [{geo}] Expected: {expected_functions}")
                                logger.error(f"[BULK] [{geo}] Submitted: {submitted_functions}")
                            else:
                                logger.info(f"[BULK] [{geo}] ✓✓✓ All {len(expected_functions)} function(s) successfully submitted for parallel processing")
                            
                            # Wait for all functions to complete and collect results
                            for future in as_completed(function_futures):
                                func_name, batch_idx = function_futures[future]
                                try:
                                    function_results = future.result()
                                    geo_results.extend(function_results)
                                    
                                    # Update job status
                                    with jobs_lock:
                                        if job_id in active_jobs:
                                            for result in function_results:
                                                active_jobs[job_id]['completed'] += 1
                                                if result.get('success'):
                                                    active_jobs[job_id]['success'] += 1
                                                    active_jobs[job_id]['results'].append({
                                                        'email': result['email'],
                                                        'app_password': result.get('app_password'),
                                                        'success': True
                                                    })
                                                else:
                                                    active_jobs[job_id]['failed'] += 1
                                                    active_jobs[job_id]['results'].append({
                                                        'email': result['email'],
                                                        'error': result.get('error', 'Unknown error'),
                                                        'success': False
                                                    })
                                    
                                    logger.info(f"[BULK] [{geo}] ✓ Function {func_name} finished: {sum(1 for r in function_results if r.get('success'))}/{len(function_results)} success")
                                except Exception as e:
                                    logger.error(f"[BULK] [{geo}] ✗ Function {func_name} exception: {e}")
                                    logger.error(traceback.format_exc())
                                    # Add failed results for all users in this batch
                                    if batch_idx < len(geo_batches_list):
                                        failed_batch_users = geo_batches_list[batch_idx][1] if len(geo_batches_list[batch_idx]) > 1 else []
                                        for u in failed_batch_users:
                                            geo_results.append({
                                                'email': u.get('email', 'unknown'),
                                                'success': False,
                                                'error': f'Function processing exception: {str(e)}'
                                            })
                        
                        # Log completion summary
                        logger.info("=" * 60)
                        logger.info(f"[BULK] [{geo}] ===== PARALLEL PROCESSING COMPLETED =====")
                        logger.info(f"[BULK] [{geo}] Total functions processed: {len(geo_batches_list)}")
                        logger.info(f"[BULK] [{geo}] Total users processed: {len(geo_results)}")
                        logger.info(f"[BULK] [{geo}] Success: {sum(1 for r in geo_results if r.get('success'))}")
                        logger.info(f"[BULK] [{geo}] Failed: {sum(1 for r in geo_results if not r.get('success'))}")
                        logger.info("=" * 60)
                        return geo_results
                    except Exception as geo_err:
                        logger.error("=" * 60)
                        logger.error(f"[BULK] [{geo}] ✗✗✗ CRITICAL ERROR in process_geo_parallel: {geo_err}")
                        logger.error(f"[BULK] [{geo}] Error type: {type(geo_err).__name__}")
                        logger.error(f"[BULK] [{geo}] Error message: {str(geo_err)}")
                        logger.error(f"[BULK] [{geo}] Traceback: {traceback.format_exc()}")
                        logger.error("=" * 60)
                        # Return empty results with error info so other geos can continue
                        # But mark all users in this geo as failed
                        failed_results = []
                        for func_name, batch_users in geo_batches_list:
                            for u in batch_users:
                                failed_results.append({
                                    'email': u['email'],
                                    'success': False,
                                    'error': f'Geo processing failed for {geo}: {str(geo_err)}'
                                })
                        logger.error(f"[BULK] [{geo}] Returning {len(failed_results)} failed results for {geo}")
                        return failed_results
            
                # Process ALL geos in parallel (each geo processes its functions in parallel internally)
                total_batches = sum(len(batches) for batches in batches_by_geo.values())
                logger.info("=" * 60)
                logger.info(f"[BULK] ===== STARTING PARALLEL GEO PROCESSING =====")
                logger.info(f"[BULK] Total users: {total_users}")
                logger.info(f"[BULK] Total batches: {total_batches}")
                logger.info(f"[BULK] Number of geos: {len(batches_by_geo)}")
                logger.info(f"[BULK] Users per function: {USERS_PER_FUNCTION}")
                logger.info(f"[BULK] Geos to process: {list(batches_by_geo.keys())}")
                logger.info("=" * 60)
            
                # Process ALL geos in parallel (each geo processes functions in parallel internally)
                max_geo_workers = len(batches_by_geo)  # One worker per geo - ALL geos process in parallel
                logger.info("=" * 60)
                logger.info(f"[BULK] ===== STARTING PARALLEL GEO PROCESSING =====")
                logger.info(f"[BULK] Total geos to process: {len(batches_by_geo)}")
                logger.info(f"[BULK] Geos: {list(batches_by_geo.keys())}")
                logger.info(f"[BULK] Max geo workers: {max_geo_workers}")
                logger.info("=" * 60)
            
                # Collect all results from all geos
                all_geo_results = []
                
                with ThreadPoolExecutor(max_workers=max_geo_workers) as geo_pool:
                    # Submit ALL geos for processing in parallel
                    geo_futures = {}
                    submitted_geos = []
                    for geo, geo_batches_list in batches_by_geo.items():
                        try:
                            logger.info(f"[BULK] ✓ Submitting geo {geo} with {len(geo_batches_list)} function(s) to thread pool")
                            future = geo_pool.submit(process_geo_parallel, geo, geo_batches_list)
                            geo_futures[future] = geo
                            submitted_geos.append(geo)
                            logger.info(f"[BULK] ✓✓✓ Successfully submitted geo {geo} to thread pool")
                        except Exception as submit_err:
                            logger.error(f"[BULK] ✗✗✗ FAILED to submit geo {geo} to thread pool: {submit_err}")
                            logger.error(traceback.format_exc())
                            # Add failed results for this geo
                            for func_name, batch_users in geo_batches_list:
                                for u in batch_users:
                                    all_geo_results.append({
                                        'email': u['email'],
                                        'success': False,
                                        'error': f'Failed to submit geo {geo} to thread pool: {str(submit_err)}'
                                    })
                
                    logger.info("=" * 60)
                    logger.info(f"[BULK] ✓✓✓ SUBMISSION SUMMARY")
                    logger.info(f"[BULK] Total geos to process: {len(batches_by_geo)}")
                    logger.info(f"[BULK] Successfully submitted: {len(submitted_geos)} geo(s)")
                    logger.info(f"[BULK] Submitted geos: {submitted_geos}")
                    logger.info(f"[BULK] Futures created: {len(geo_futures)}")
                    logger.info(f"[BULK] All geos should now be processing simultaneously")
                    logger.info("=" * 60)
                
                    # Wait for all geos to complete and collect results
                    logger.info("=" * 60)
                    logger.info(f"[BULK] ===== WAITING FOR GEO FUTURES TO COMPLETE =====")
                    logger.info(f"[BULK] Total futures to wait for: {len(geo_futures)}")
                    logger.info(f"[BULK] Using as_completed() to wait for results...")
                    logger.info("=" * 60)
                    
                    completed_geos = []
                    failed_geos = []
                    future_count = 0
                    for future in as_completed(geo_futures):
                        future_count += 1
                        geo = geo_futures[future]
                        logger.info(f"[BULK] Future {future_count}/{len(geo_futures)} completed for geo: {geo}")
                        try:
                            logger.info(f"[BULK] [{geo}] Getting result from future...")
                            geo_results = future.result(timeout=3600)  # 1 hour timeout per geo
                            logger.info(f"[BULK] [{geo}] ✓ Got result from future: {len(geo_results)} results")
                            all_geo_results.extend(geo_results)
                            completed_geos.append(geo)
                            success_count = sum(1 for r in geo_results if r.get('success'))
                            total_count = len(geo_results)
                            failed_count = total_count - success_count
                            logger.info("=" * 60)
                            logger.info(f"[BULK] [{geo}] ✓✓✓ GEO COMPLETED: {success_count}/{total_count} success, {failed_count} failed")
                            logger.info(f"[BULK] [{geo}] Functions processed: {len(geo_batches_list)}")
                            logger.info(f"[BULK] [{geo}] Results count: {len(geo_results)}")
                            logger.info(f"[BULK] Completed geos so far: {len(completed_geos)}/{len(geo_futures)}")
                            if failed_count > 0:
                                logger.warning(f"[BULK] [{geo}] ⚠️ Some failures detected: {failed_count} user(s) failed")
                            logger.info("=" * 60)
                        except Exception as e:
                            logger.error("=" * 60)
                            logger.error(f"[BULK] [{geo}] ✗✗✗ GEO EXCEPTION (Future Error): {e}")
                            logger.error(f"[BULK] [{geo}] Error type: {type(e).__name__}")
                            logger.error(f"[BULK] [{geo}] Traceback: {traceback.format_exc()}")
                            logger.error("=" * 60)
                            failed_geos.append(geo)
                            completed_geos.append(geo)  # Mark as completed even if failed
                            
                            # Add failed results for all users in this geo
                            if geo in batches_by_geo:
                                for func_name, batch_users in batches_by_geo[geo]:
                                    for u in batch_users:
                                        all_geo_results.append({
                                            'email': u['email'],
                                            'success': False,
                                            'error': f'Geo processing exception for {geo}: {str(e)}'
                                        })
                
                    logger.info("=" * 60)
                    logger.info(f"[BULK] ===== ALL GEOS COMPLETED PROCESSING =====")
                    logger.info(f"[BULK] Total geos processed: {len(completed_geos)}/{len(geo_futures)}")
                    logger.info(f"[BULK] Completed geos: {completed_geos}")
                    logger.info(f"[BULK] Total results collected: {len(all_geo_results)}")
                    logger.info("=" * 60)
            
            # Set job status to completed (outside ThreadPoolExecutor but inside app_context)
            # Use lock to ensure thread-safe access
            with jobs_lock:
                if job_id in active_jobs:
                    completed_count = active_jobs[job_id].get('completed', 0)
                    success_count = active_jobs[job_id].get('success', 0)
                    failed_count = active_jobs[job_id].get('failed', 0)
                    active_jobs[job_id]['status'] = 'completed'
                    logger.info(f"[BULK] ✅ Job {job_id} completed successfully. Processed {completed_count}/{len(users)} users. Success: {success_count}, Failed: {failed_count}")
                else:
                    logger.error(f"[BULK] ⚠️ Job {job_id} not found in active_jobs when trying to mark as completed!")
                    # Try to create it if it doesn't exist (shouldn't happen, but safety check)
                    # Since we don't have the actual counts, use defaults
                    active_jobs[job_id] = {
                        'total': len(users),
                        'completed': 0,
                        'success': 0,
                        'failed': len(users),
                        'results': [],
                        'status': 'completed'
                    }
                    logger.warning(f"[BULK] Created fallback job entry for {job_id} with default values")
        except Exception as bg_error:
            print(f"[BACKGROUND_ERROR] CRITICAL ERROR in background_process: {bg_error}", flush=True)
            print(traceback.format_exc(), flush=True)
            logger.error("=" * 80)
            logger.error(f"[BULK] ❌❌❌ CRITICAL ERROR in background_process: {bg_error}")
            logger.error(f"[BULK] Error type: {type(bg_error).__name__}")
            logger.error(f"[BULK] Traceback: {traceback.format_exc()}")
            logger.error("=" * 80)
            # Use lock to ensure thread-safe access
            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'failed'
                    active_jobs[job_id]['error'] = str(bg_error)
                    active_jobs[job_id]['completed'] = active_jobs[job_id].get('completed', 0)
                else:
                    logger.error(f"[BULK] ⚠️ Job {job_id} not found in active_jobs when trying to mark as failed!")
            import sys
            sys.stdout.flush()
            sys.stderr.flush()

    # Start background thread with proper exception handling
    def safe_background_wrapper():
        """Wrapper to catch any exceptions during thread startup"""
        try:
            print(f"[THREAD_START] Starting background thread for job {job_id}", flush=True)
            logger.info(f"[BULK] Thread wrapper: About to call background_process for job {job_id}")
            background_process(app, job_id, users, access_key, secret_key, region, lambda_prefix_bulk, lambda_role_name_bulk, dynamodb_table_bulk, users_per_function)
            logger.info(f"[BULK] Thread wrapper: background_process completed for job {job_id}")
            print(f"[THREAD_START] Background thread completed for job {job_id}", flush=True)
        except Exception as thread_start_err:
            print(f"[THREAD_START] CRITICAL ERROR in thread wrapper: {thread_start_err}", flush=True)
            print(traceback.format_exc(), flush=True)
            logger.error(f"[BULK] ❌❌❌ CRITICAL ERROR in thread wrapper: {thread_start_err}")
            logger.error(f"[BULK] Thread wrapper traceback: {traceback.format_exc()}")
            # Mark job as failed
            with jobs_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'failed'
                    active_jobs[job_id]['error'] = f'Thread startup error: {str(thread_start_err)}'
                else:
                    logger.error(f"[BULK] Job {job_id} not found when trying to mark thread error!")
    
    try:
        thread = threading.Thread(target=safe_background_wrapper, daemon=False, name=f"BulkProcess-{job_id}")
        thread.start()
        logger.info(f"[BULK] ✓ Background thread started: {thread.name} (ID: {thread.ident})")
        print(f"[THREAD_START] Thread object created and started: {thread.name}", flush=True)
    except Exception as thread_err:
        logger.error(f"[BULK] ❌❌❌ FAILED to start background thread: {thread_err}")
        logger.error(f"[BULK] Thread start traceback: {traceback.format_exc()}")
        print(f"[THREAD_START] FAILED to start thread: {thread_err}", flush=True)
        print(traceback.format_exc(), flush=True)
        # Mark job as failed
        with jobs_lock:
            if job_id in active_jobs:
                active_jobs[job_id]['status'] = 'failed'
                active_jobs[job_id]['error'] = f'Failed to start thread: {str(thread_err)}'

    return jsonify({'success': True, 'job_id': job_id, 'message': f'Started processing {len(users)} users'})

@aws_manager.route('/api/aws/lambda-creation-status/<job_id>', methods=['GET'])
@login_required
def get_lambda_creation_status(job_id):
    """Get status of Lambda function creation job"""
    try:
        with lambda_creation_lock:
            if job_id not in lambda_creation_jobs:
                return jsonify({'success': False, 'error': 'Creation job not found'}), 404
            
            job = lambda_creation_jobs[job_id].copy()
            # Calculate elapsed time
            if 'started_at' in job:
                elapsed = time.time() - job['started_at']
                job['elapsed_seconds'] = int(elapsed)
            
            return jsonify({
                'success': True,
                'job': job
            })
    except Exception as e:
        logger.error(f"Error getting Lambda creation status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/job-status/<job_id>', methods=['GET'])
@login_required
def get_job_status(job_id):
    try:
        with jobs_lock:
            # Load from file to get latest status from any worker
            all_jobs = load_jobs()
            job = all_jobs.get(job_id)
        if not job:
            logger.warning(f"[JOB_STATUS] Job {job_id} not found. Available jobs: {list(active_jobs.keys())}")
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        # Return the job status including the results list (which has the new passwords)
        return jsonify({'success': True, 'job': job})
    except Exception as e:
        logger.error(f"Error getting job status: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/fetch-from-dynamodb', methods=['POST'])
@login_required
def fetch_from_dynamodb():
    """Fetch app passwords from DynamoDB for specific users
    Uses centralized DynamoDB table in eu-west-1 (all Lambda functions save to same table)
    """
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        emails = data.get('emails', [])  # List of emails to fetch
        
        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400
        
        if not emails:
            return jsonify({'success': False, 'error': 'No emails provided'}), 400
        
        # Get configurable DynamoDB table name from request (preferred) or database (fallback)
        table_name = data.get('dynamodb_table', '').strip() or get_naming_config().get('dynamodb_table', DEFAULT_DYNAMODB_TABLE)
        
        # Centralized DynamoDB region - all Lambda functions save to this single table
        # This saves resources (1 table instead of 17 tables)
        dynamodb_region = "eu-west-1"  # Fixed region for centralized storage
        
        logger.info(f"[DYNAMODB] Fetching {len(emails)} email(s) from DynamoDB table '{table_name}' in {dynamodb_region} (centralized storage)...")
        
        session = get_boto3_session(access_key, secret_key, dynamodb_region)
        dynamodb = session.resource('dynamodb')
        
        try:
            table = dynamodb.Table(table_name)
            # LOG TABLE ITEM COUNT - TRACKING DATA DISAPPEARANCE ISSUE
            table_scan = table.scan(Select='COUNT')
            item_count_before = table_scan.get('Count', 0)
            logger.info(f"[DYNAMODB] ⚠️ TABLE STATUS BEFORE FETCH: {table_name} has {item_count_before} items")
        except Exception as e:
            return jsonify({'success': False, 'error': f'DynamoDB table {table_name} not found in {dynamodb_region}: {e}'}), 404
        
        # Use batch_get_item for parallel fetching (much faster than sequential get_item)
        # DynamoDB batch_get_item can fetch up to 100 items at once
        results = []
        dynamodb_client = session.client('dynamodb')
        
        # Process emails in batches of 100 (DynamoDB limit) using ThreadPoolExecutor for parallel batches
        batch_size = 100
        
        def fetch_batch(email_batch):
            """Fetch a batch of emails from DynamoDB"""
            batch_results = []
            try:
                # Prepare keys for batch_get_item (DynamoDB client format - low-level API)
                keys = [{'email': {'S': email}} for email in email_batch]
                
                # Use batch_get_item (faster than individual get_item calls)
                response = dynamodb_client.batch_get_item(
                    RequestItems={
                        table_name: {
                            'Keys': keys
                        }
                    }
                )
                
                # Process results (low-level API returns DynamoDB format)
                items = response.get('Responses', {}).get(table_name, [])
                found_emails = set()
                
                for item in items:
                    email = item['email']['S']
                    app_password = item['app_password']['S']
                    found_emails.add(email)
                    
                    # Save to local AwsGeneratedPassword table
                    try:
                        save_app_password(email, app_password)
                    except Exception as db_err:
                        logger.warning(f"[DYNAMODB] Could not save to local DB for {email}: {db_err}")
                    
                    batch_results.append({
                        'email': email,
                        'app_password': app_password,
                        'created_at': item.get('created_at', {}).get('S', ''),
                        'region': dynamodb_region,
                        'success': True
                    })
                
                # Mark emails not found in this batch
                for email in email_batch:
                    if email not in found_emails:
                        batch_results.append({
                            'email': email,
                            'error': 'Not found in DynamoDB',
                            'success': False
                        })
                            
            except Exception as e:
                logger.error(f"[DYNAMODB] Error in batch fetch: {e}")
                logger.error(traceback.format_exc())
                # Fallback to individual get_item for this batch
                for email in email_batch:
                    try:
                        response = table.get_item(Key={'email': email})
                        if 'Item' in response:
                            item = response['Item']
                            app_password = item['app_password']
                            
                            try:
                                save_app_password(email, app_password)
                            except Exception as db_err:
                                logger.warning(f"[DYNAMODB] Could not save to local DB for {email}: {db_err}")
                            
                            batch_results.append({
                                'email': item['email'],
                                'app_password': app_password,
                                'created_at': item.get('created_at', ''),
                                'region': dynamodb_region,
                                'success': True
                            })
                        else:
                            batch_results.append({
                                'email': email,
                                'error': 'Not found in DynamoDB',
                                'success': False
                            })
                    except Exception as get_err:
                        logger.error(f"[DYNAMODB] Error fetching {email}: {get_err}")
                        batch_results.append({
                            'email': email,
                            'error': str(get_err),
                            'success': False
                        })
        
            return batch_results
        
        # Process all batches in parallel using ThreadPoolExecutor
        logger.info(f"[DYNAMODB] Processing {len(emails)} emails in {len(emails) // batch_size + 1} batch(es) in parallel...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            email_batches = [emails[i:i + batch_size] for i in range(0, len(emails), batch_size)]
            futures = [executor.submit(fetch_batch, batch) for batch in email_batches]
            
            for future in as_completed(futures):
                try:
                    batch_results = future.result()
                    results.extend(batch_results)
                except Exception as e:
                    logger.error(f"[DYNAMODB] Error in batch future: {e}")
                    logger.error(traceback.format_exc())
        
        success_count = sum(1 for r in results if r.get('success'))
        logger.info(f"[DYNAMODB] Fetch complete: {success_count}/{len(emails)} found")
        
        # LOG TABLE ITEM COUNT AFTER FETCH - TRACKING DATA DISAPPEARANCE ISSUE
        try:
            table_scan_after = table.scan(Select='COUNT')
            item_count_after = table_scan_after.get('Count', 0)
            logger.info(f"[DYNAMODB] ⚠️ TABLE STATUS AFTER FETCH: {table_name} has {item_count_after} items")
            if item_count_after < item_count_before:
                logger.critical(f"[DYNAMODB] 🚨 DATA LOSS DETECTED! Table had {item_count_before} items before fetch, now has {item_count_after} items!")
        except Exception as count_err:
            item_count_after = -1
            logger.warning(f"[DYNAMODB] Could not get item count after fetch: {count_err}")
        
        return jsonify({
            'success': True, 
            'results': results,
            'summary': {
                'total': len(emails),
                'found': success_count,
                'not_found': len(emails) - success_count
            },
            'table_info': {
                'table_name': table_name,
                'items_before_fetch': item_count_before,
                'items_after_fetch': item_count_after
            }
        })
    except Exception as e:
        logger.error(f"Error fetching from DynamoDB: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/generated-passwords', methods=['GET'])
@login_required
def get_generated_passwords():
    """Fetch all generated app passwords from local DB (deprecated - use DynamoDB)"""
    try:
        # Get recent passwords from AwsGeneratedPassword table
        passwords = AwsGeneratedPassword.query.order_by(AwsGeneratedPassword.created_at.desc()).all()
        result = []
        for p in passwords:
            result.append({
                'email': p.email,
                'app_password': p.app_password,
                'created_at': p.created_at.isoformat()
            })
        return jsonify({'success': True, 'passwords': result})
    except Exception as e:
        # If table doesn't exist or other DB error, return empty list to prevent frontend crash
        logger.error(f"Error fetching generated passwords: {e}")
        return jsonify({'success': True, 'passwords': [], 'error': str(e)})

def save_app_password(email, app_password):
    """Save app password to AwsGeneratedPassword table"""
    try:
        logger.info(f"[DB] Attempting to save password for {email}")
        # Check if exists
        existing = AwsGeneratedPassword.query.filter_by(email=email).first()
        if existing:
            logger.info(f"[DB] Updating existing entry for {email}")
            existing.app_password = app_password
            existing.updated_at = db.func.current_timestamp()
        else:
            logger.info(f"[DB] Creating new entry for {email}")
            new_entry = AwsGeneratedPassword(email=email, app_password=app_password)
            db.session.add(new_entry)
        
        db.session.commit()
        logger.info(f"[DB] ✓ Successfully saved password for {email}")
    except Exception as e:
        db.session.rollback()
        logger.error(f"[DB] ✗ Error saving app password for {email}: {e}")
        logger.error(f"[DB] Exception details: {traceback.format_exc()}")

# --- End Bulk Logic ---

@aws_manager.route('/api/aws/invoke-lambda', methods=['POST'])
@login_required
def invoke_lambda():
    """Invoke production Lambda (Single invocation)"""
    # Allow admin, mailer, and support to invoke functions
    allowed_roles = ['admin', 'mailer', 'support']
    if str(session.get('role', '')).lower() not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        email = data.get('email', '').strip()
        password = data.get('password', '').strip()
        async_mode = data.get('async', False)

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        if not email or not password:
            return jsonify({'success': False, 'error': 'Please provide email and password.'}), 400

        # [MULTI-USER] Get lambda_prefix based on logged-in user
        # Priority: 1) Request data, 2) Session user dynamic prefix, 3) Database config
        _session_user = session.get('user')
        clean_username = _session_user.split('@')[0].lower() if _session_user else None
        naming_config = get_naming_config(clean_user=clean_username)

        if data.get('lambda_prefix', '').strip():
            lambda_prefix_invoke = data.get('lambda_prefix').strip()
        elif clean_username:
            lambda_prefix_invoke = naming_config.get('lambda_prefix', f"{clean_username}-chromium")
        else:
            lambda_prefix_invoke = naming_config.get('production_lambda_name', 'gbot-chromium')
            
        # [MULTI-USER] Get dynamodb_table based on logged-in user
        if data.get('dynamodb_table', '').strip():
            dynamodb_table_invoke = data.get('dynamodb_table').strip()
        else:
            dynamodb_table_invoke = naming_config.get('dynamodb_table', 'dev-app-passwords')
        
        logger.info(f"[INVOKE] Using lambda_prefix: {lambda_prefix_invoke} (user: {_session_user})")
        logger.info(f"[INVOKE] Using dynamodb_table: {dynamodb_table_invoke}")

        # NOTE: Use boto3_session to avoid shadowing Flask's session object!
        boto3_session = get_boto3_session(access_key, secret_key, region)
        lam = boto3_session.client("lambda")

        # Determine which Lambda function to use
        lambda_function_name = lambda_prefix_invoke  # Use prefix as base name
        lambda_region = region  # Track which region the function is in
        
        try:
            # List all Lambda functions that match our pattern in the specified region
            logger.info(f"[INVOKE] Searching for Lambda functions with prefix '{lambda_prefix_invoke}' in region: {region}")
            all_functions = lam.list_functions()
            matching_functions = [
                fn['FunctionName'] for fn in all_functions.get('Functions', [])
                if fn['FunctionName'].startswith(lambda_prefix_invoke)
            ]
            
            logger.info(f"[INVOKE] Found {len(matching_functions)} matching function(s) in {region}: {matching_functions}")
            
            if len(matching_functions) > 1:
                # Multiple functions exist - use hash to pick one consistently
                user_hash = int(hashlib.md5(email.encode()).hexdigest(), 16)
                function_index = user_hash % len(matching_functions)
                lambda_function_name = matching_functions[function_index]
                logger.info(f"[INVOKE] Using Lambda function {lambda_function_name} in {region} for {email} (distributed across {len(matching_functions)} functions)")
            elif len(matching_functions) == 1:
                # Only one function exists, use it
                lambda_function_name = matching_functions[0]
                logger.info(f"[INVOKE] Using Lambda function {lambda_function_name} in {region} for {email}")
            else:
                # No matching functions found in this region - try to find in other regions
                logger.warning(f"[INVOKE] No matching Lambda functions found in {region}, searching all geo regions...")
                
                # Use the full AVAILABLE_GEO_REGIONS list to find Lambdas in any geo
                search_regions = AVAILABLE_GEO_REGIONS
                
                found_function = False
                for search_region in search_regions:
                    if search_region == region:
                        continue  # Skip the region we already checked
                    try:
                        search_session = get_boto3_session(access_key, secret_key, search_region)
                        search_lam = search_session.client("lambda")
                        search_functions = search_lam.list_functions()
                        search_matching = [
                            fn['FunctionName'] for fn in search_functions.get('Functions', [])
                            if fn['FunctionName'].startswith(lambda_prefix_invoke)
                        ]
                        if search_matching:
                            lambda_function_name = search_matching[0]  # Use first found
                            lambda_region = search_region
                            logger.info(f"[INVOKE] Found Lambda function {lambda_function_name} in {search_region}, using it")
                            lam = search_lam  # Update Lambda client to use the correct region
                            found_function = True
                            break
                    except Exception as search_err:
                        logger.debug(f"[INVOKE] Could not search region {search_region}: {search_err}")
                        continue
                
                if not found_function:
                    logger.warning(f"[INVOKE] No matching Lambda functions found in any region, using default {PRODUCTION_LAMBDA_NAME} in {region}")
        except Exception as list_err:
            logger.error(f"[INVOKE] Error listing Lambda functions: {list_err}")
            logger.error(traceback.format_exc())
            logger.warning(f"[INVOKE] Using default {PRODUCTION_LAMBDA_NAME} in {region}")

        # Verify the function exists before invoking
        try:
            logger.info(f"[INVOKE] Verifying Lambda function {lambda_function_name} exists in {lambda_region}...")
            func_info = lam.get_function(FunctionName=lambda_function_name)
            func_state = func_info.get('Configuration', {}).get('State', 'Unknown')
            logger.info(f"[INVOKE] Lambda function {lambda_function_name} state: {func_state}")
            if func_state != 'Active':
                logger.warning(f"[INVOKE] Lambda function is not in Active state: {func_state}")
        except ClientError as verify_err:
            error_code = verify_err.response.get('Error', {}).get('Code', '')
            if error_code == 'ResourceNotFoundException':
                logger.error(f"[INVOKE] Lambda function {lambda_function_name} not found in {lambda_region}")
                return jsonify({
                    'success': False,
                    'error': f'Lambda function {lambda_function_name} not found in region {lambda_region}. Please create Lambda functions first.'
                }), 404
            else:
                logger.error(f"[INVOKE] Error verifying function: {verify_err}")
                raise

        event = {
            "email": email,
            "password": password,
            "dynamodb_table": dynamodb_table_invoke,
        }

        logger.info(f"[INVOKE] Invoking Lambda function {lambda_function_name} in {lambda_region} (async={async_mode})...")
        logger.info(f"[INVOKE] Event payload: {json.dumps(event)}")
        
        invocation_type = "Event" if async_mode else "RequestResponse"
        
        try:
            resp = lam.invoke(
                FunctionName=lambda_function_name,
                InvocationType=invocation_type,
                Payload=json.dumps(event).encode("utf-8"),
            )
            
            status_code = resp.get("StatusCode", 0)
            function_error = resp.get("FunctionError")
            executed_version = resp.get("ExecutedVersion")
            
            logger.info(f"[INVOKE] Lambda invocation response:")
            logger.info(f"[INVOKE]   Status Code: {status_code}")
            logger.info(f"[INVOKE]   Function Error: {function_error}")
            logger.info(f"[INVOKE]   Executed Version: {executed_version}")
            
            # Check for function errors
            if function_error:
                payload = resp.get("Payload")
                error_body = payload.read().decode("utf-8") if payload else ""
                logger.error(f"[INVOKE] Lambda function error ({function_error}): {error_body}")
                try:
                    error_data = json.loads(error_body)
                    error_message = error_data.get('errorMessage', error_data.get('errorType', 'Unknown error'))
                    error_type = error_data.get('errorType', 'FunctionError')
                    return jsonify({
                        'success': False,
                        'error': f'Lambda function error ({error_type}): {error_message}',
                        'error_type': error_type,
                        'error_message': error_message,
                        'full_error': error_body
                    }), 500
                except:
                    return jsonify({
                        'success': False,
                        'error': f'Lambda function error: {error_body}',
                        'raw_error': error_body
                    }), 500

            if async_mode:
                if status_code == 202:
                    logger.info(f"[INVOKE] ✓ Lambda invoked asynchronously (status 202)")
                    return jsonify({
                        'success': True,
                        'status': 'invoked',
                        'message': 'Lambda invoked asynchronously',
                        'function_name': lambda_function_name,
                        'region': lambda_region
                    })
                else:
                    logger.error(f"[INVOKE] Unexpected status code for async: {status_code}")
                    return jsonify({
                        'success': False,
                        'error': f'Unexpected status code: {status_code}',
                        'function_name': lambda_function_name,
                        'region': lambda_region
                    }), 500
            else:
                payload = resp.get("Payload")
                body = payload.read().decode("utf-8") if payload else ""
                logger.info(f"[INVOKE] Response body length: {len(body)} characters")
                logger.info(f"[INVOKE] Response body preview: {body[:500]}")
                
                if not body:
                    logger.warning(f"[INVOKE] Empty response body from Lambda")
                    return jsonify({
                        'success': False,
                        'error': 'Lambda returned empty response',
                        'status_code': status_code,
                        'function_name': lambda_function_name,
                        'region': lambda_region
                    }), 500
                
                try:
                    response_data = json.loads(body)
                    logger.info(f"[INVOKE] Parsed response: {json.dumps(response_data, indent=2)[:500]}")
                    
                    # Check if Lambda returned an error in the response
                    if response_data.get('status') == 'error' or response_data.get('error'):
                        error_msg = response_data.get('error', response_data.get('error_message', 'Unknown error'))
                        logger.error(f"[INVOKE] Lambda returned error in response: {error_msg}")
                        return jsonify({
                            'success': False,
                            'error': error_msg,
                            'function_name': lambda_function_name,
                            'region': lambda_region,
                            'lambda_response': response_data
                        }), 500
                    
                    # Save to DB if successful
                    if response_data.get('app_password'):
                        try:
                            save_app_password(email, response_data['app_password'])
                            logger.info(f"[INVOKE] ✓ Password saved for {email}")
                        except Exception as db_error:
                            logger.error(f"[INVOKE] Failed to save password to DB: {db_error}")
                            # Continue anyway - return the password even if DB save fails
                    
                    logger.info(f"[INVOKE] ✓ Lambda invocation successful")
                    return jsonify({
                        'success': True,
                        'function_name': lambda_function_name,
                        'region': lambda_region,
                        **response_data
                    })
                except json.JSONDecodeError as parse_error:
                    logger.error(f"[INVOKE] Failed to parse response as JSON: {parse_error}")
                    logger.error(f"[INVOKE] Raw response: {body}")
                    return jsonify({
                        'success': False,
                        'error': f'Failed to parse Lambda response: {str(parse_error)}',
                        'raw_response': body,
                        'function_name': lambda_function_name,
                        'region': lambda_region
                    }), 500
        except ClientError as invoke_err:
            error_code = invoke_err.response.get('Error', {}).get('Code', '')
            error_message = invoke_err.response.get('Error', {}).get('Message', str(invoke_err))
            logger.error(f"[INVOKE] AWS ClientError during invocation: {error_code} - {error_message}")
            logger.error(traceback.format_exc())
            return jsonify({
                'success': False,
                'error': f'AWS Error ({error_code}): {error_message}',
                'function_name': lambda_function_name,
                'region': lambda_region
            }), 500
    except ClientError as ce:
        if ce.response['Error']['Code'] == 'ResourceNotFoundException':
            return jsonify({
                'success': False,
                'error': f'Production Lambda {PRODUCTION_LAMBDA_NAME} not found. Create it first.'
            }), 404
        return jsonify({'success': False, 'error': str(ce)}), 500
    except Exception as e:
        logger.error(f"Error invoking Lambda: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/stop-all-lambdas', methods=['POST'])
@login_required
def stop_all_lambdas():
    """Stop all running Lambda function executions across all AWS regions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        
        # Get lambda prefix from request (from UI input field)
        lambda_prefix_from_request = data.get('lambda_prefix', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # Use lambda prefix from request (UI input), fallback to config only if not provided
        if lambda_prefix_from_request:
            lambda_prefix_stop = lambda_prefix_from_request
            logger.info(f"[STOP LAMBDA] Using lambda prefix from UI: {lambda_prefix_stop}")
        else:
            naming_config_stop = get_naming_config()
            lambda_prefix_stop = naming_config_stop.get('production_lambda_name', 'dev-chromium')
            logger.info(f"[STOP LAMBDA] Using lambda prefix from config: {lambda_prefix_stop}")

        # List of all AWS regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]

        stopped_count = 0
        error_count = 0
        all_errors = []

        def stop_region_lambdas(target_region):
            """Stop all running Lambda executions in a specific region (parallel execution)"""
            region_stopped = 0
            region_errors = []
            try:
                logger.info(f"[STOP LAMBDA] Processing region: {target_region}")
                session = get_boto3_session(access_key, secret_key, target_region)
                lam = session.client("lambda")

                # List all Lambda functions matching our pattern
                try:
                    paginator = lam.get_paginator("list_functions")
                    for page in paginator.paginate():
                        for fn in page.get("Functions", []):
                            fn_name = fn["FunctionName"]
                            # Use the lambda prefix from the request/UI
                            if lambda_prefix_stop in fn_name:
                                try:
                                    # Put reserved concurrency to 0 to stop new invocations
                                    # This effectively stops the function from accepting new requests
                                    # Also update function configuration to disable it
                                    try:
                                        lam.put_function_concurrency(
                                            FunctionName=fn_name,
                                            ReservedConcurrentExecutions=0
                                        )
                                        logger.info(f"[STOP LAMBDA] [{target_region}] ✓ Set concurrency to 0 for: {fn_name}")
                                    except ClientError as concurrency_err:
                                        # If concurrency update fails, try to get current config and update
                                        logger.warning(f"[STOP LAMBDA] [{target_region}] Concurrency update failed for {fn_name}: {concurrency_err}")
                                    
                                    # Also try to update function configuration to disable it
                                    try:
                                        lam.update_function_configuration(
                                            FunctionName=fn_name,
                                            Description="Stopped - reserved concurrency set to 0"
                                        )
                                    except Exception as config_err:
                                        # Config update is optional, just log warning
                                        logger.debug(f"[STOP LAMBDA] [{target_region}] Config update optional, continuing: {config_err}")
                                    
                                    region_stopped += 1
                                    logger.info(f"[STOP LAMBDA] [{target_region}] ✓ Stopped function: {fn_name}")
                                except Exception as e:
                                    error_msg = f"{target_region}: {fn_name} - {str(e)}"
                                    logger.error(f"[STOP LAMBDA] [{target_region}] Error stopping {fn_name}: {e}")
                                    region_errors.append(error_msg)
                except Exception as e:
                    error_msg = f"{target_region}: Failed to list functions - {str(e)}"
                    logger.error(f"[STOP LAMBDA] [{target_region}] Error listing functions: {e}")
                    region_errors.append(error_msg)

            except Exception as e:
                error_msg = f"{target_region}: Region processing failed - {str(e)}"
                logger.error(f"[STOP LAMBDA] [{target_region}] Error: {e}")
                region_errors.append(error_msg)
            
            return region_stopped, region_errors

        # Process all regions in parallel
        logger.info(f"[STOP LAMBDA] Starting parallel stop across {len(AVAILABLE_GEO_REGIONS)} regions...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(stop_region_lambdas, region): region for region in AVAILABLE_GEO_REGIONS}
            
            for future in as_completed(futures):
                region = futures[future]
                try:
                    region_stopped, region_errors = future.result()
                    stopped_count += region_stopped
                    all_errors.extend(region_errors)
                    error_count += len(region_errors)
                except Exception as e:
                    logger.error(f"[STOP LAMBDA] [{region}] Future error: {e}")
                    error_count += 1
                    all_errors.append(f"{region}: Future execution error - {str(e)}")

        return jsonify({
            'success': True,
            'stopped_count': stopped_count,
            'error_count': error_count,
            'errors': all_errors if all_errors else None,
            'message': f'Lambda stop completed across all regions. Stopped: {stopped_count} function(s), Errors: {error_count}'
        })
    except Exception as e:
        logger.error(f"Error stopping Lambdas: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/delete-all-lambdas', methods=['POST'])
@login_required
def delete_all_lambdas():
    """Delete all production Lambdas across all AWS regions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        
        # Get lambda prefix from request (from UI input field)
        lambda_prefix_from_request = data.get('lambda_prefix', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # List of all AWS regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]

        all_deleted = []
        all_errors = []
        total_deleted = 0
        total_errors = 0

        # Use lambda prefix from request (UI input), fallback to config only if not provided
        if lambda_prefix_from_request:
            lambda_prefix_delete = lambda_prefix_from_request
            logger.info(f"[DELETE LAMBDA] Using lambda prefix from UI: {lambda_prefix_delete}")
        else:
            naming_config_delete = get_naming_config()
            lambda_prefix_delete = naming_config_delete.get('production_lambda_name', 'dev-chromium')
            logger.info(f"[DELETE LAMBDA] Using lambda prefix from config: {lambda_prefix_delete}")
        
        def delete_region_lambdas(target_region):
            """Delete all Lambda functions in a specific region (parallel execution)"""
            region_deleted = []
            region_errors = []
            try:
                logger.info(f"[DELETE LAMBDA] Processing region: {target_region}")
                session = get_boto3_session(access_key, secret_key, target_region)
                lam = session.client("lambda")
    
                # Try to delete production lambda
                try:
                    lam.delete_function(FunctionName=lambda_prefix_delete)
                    region_deleted.append(f"{lambda_prefix_delete} ({target_region})")
                except lam.exceptions.ResourceNotFoundException:
                    pass
                except Exception as e:
                    error_msg = f"{target_region}: {lambda_prefix_delete} - {str(e)}"
                    logger.error(f"[DELETE LAMBDA] [{target_region}] Error: {error_msg}")
                    region_errors.append(error_msg)

        # Also check for lambdas with the configured prefix
                try:
                    paginator = lam.get_paginator("list_functions")
                    for page in paginator.paginate():
                        for fn in page.get("Functions", []):
                            fn_name = fn["FunctionName"]
                            # Delete functions matching the configured prefix or legacy edu-gw prefix
                            if lambda_prefix_delete in fn_name or "gbot" in fn_name.lower():
                                try:
                                    lam.delete_function(FunctionName=fn_name)
                                    region_deleted.append(f"{fn_name} ({target_region})")
                                except lam.exceptions.ResourceNotFoundException:
                                    pass
                                except Exception as e:
                                    error_msg = f"{target_region}: {fn_name} - {str(e)}"
                                    logger.error(f"[DELETE LAMBDA] [{target_region}] Error deleting {fn_name}: {e}")
                                    region_errors.append(error_msg)
                except Exception as e:
                    error_msg = f"{target_region}: Failed to list functions - {str(e)}"
                    logger.error(f"[DELETE LAMBDA] [{target_region}] Error listing functions: {e}")
                    region_errors.append(error_msg)

                if region_deleted:
                    logger.info(f"[DELETE LAMBDA] [{target_region}] Deleted {len(region_deleted)} function(s)")

            except Exception as e:
                error_msg = f"{target_region}: Region processing failed - {str(e)}"
                logger.error(f"[DELETE LAMBDA] [{target_region}] Error: {e}")
                region_errors.append(error_msg)
            
            return region_deleted, region_errors

        # Process all regions in parallel
        logger.info(f"[DELETE LAMBDA] Starting parallel deletion across {len(AVAILABLE_GEO_REGIONS)} regions...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(delete_region_lambdas, region): region for region in AVAILABLE_GEO_REGIONS}
            
            for future in as_completed(futures):
                region = futures[future]
                try:
                    region_deleted, region_errors = future.result()
                    all_deleted.extend(region_deleted)
                    all_errors.extend(region_errors)
                    total_deleted += len(region_deleted)
                    total_errors += len(region_errors)
                except Exception as e:
                    logger.error(f"[DELETE LAMBDA] [{region}] Future error: {e}")
                    total_errors += 1
                    all_errors.append(f"{region}: Future execution error - {str(e)}")

        return jsonify({
            'success': True,
            'deleted': all_deleted,
            'deleted_count': total_deleted,
            'error_count': total_errors,
            'errors': all_errors if all_errors else None,
            'message': f'Lambda cleanup completed across all regions. Deleted: {total_deleted}, Errors: {total_errors}'
        })
    except Exception as e:
        logger.error(f"Error deleting Lambdas: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/delete-s3-content', methods=['POST'])
@login_required
def delete_s3_content():
    """Delete all contents from S3 bucket
    
    Required AWS IAM permissions:
    - s3:ListBucket (to list objects)
    - s3:DeleteObject (to delete objects)
    - s3:ListBucketVersions (if versioning is enabled)
    - s3:DeleteObjectVersion (if versioning is enabled)
    """
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # Get bucket name from request (preferred) or database (fallback)
        bucket_name = data.get('bucket_name', '').strip() or get_naming_config().get('s3_bucket', 'gbot-app-passwords')
        logger.info(f"[S3 DELETE] Using bucket name from REQUEST: {bucket_name}")

        session = get_boto3_session(access_key, secret_key, region)
        s3 = session.client("s3")

        # First, check if bucket exists and we have ListBucket permission
        try:
            s3.head_bucket(Bucket=bucket_name)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == '404' or error_code == 'NoSuchBucket':
                return jsonify({
                    'success': False,
                    'error': f'S3 bucket {bucket_name} does not exist.'
                }), 404
            elif error_code == '403' or 'AccessDenied' in str(e):
                return jsonify({
                    'success': False,
                    'error': f'Access Denied to S3 bucket {bucket_name}. Your AWS credentials need the following IAM permissions:\n'
                             f'- s3:ListBucket\n'
                             f'- s3:DeleteObject\n'
                             f'- s3:ListBucketVersions (if versioning enabled)\n'
                             f'- s3:DeleteObjectVersion (if versioning enabled)\n\n'
                             f'You can attach the "AmazonS3FullAccess" policy to your IAM user, or create a custom policy with these permissions for bucket "{bucket_name}".'
                }), 403
            else:
                raise e

        deleted_count = 0
        
        # Delete regular objects
        try:
            paginator = s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket_name):
                objects = page.get('Contents', [])
                if objects:
                    delete_keys = [{'Key': obj['Key']} for obj in objects]
                    try:
                        s3.delete_objects(
                            Bucket=bucket_name,
                            Delete={'Objects': delete_keys}
                        )
                        deleted_count += len(delete_keys)
                        logger.info(f"[S3] Deleted {len(delete_keys)} objects from {bucket_name}")
                    except ClientError as delete_err:
                        error_code = delete_err.response.get('Error', {}).get('Code', '')
                        if error_code == 'AccessDenied':
                            return jsonify({
                                'success': False,
                                'error': f'Access Denied when deleting objects. Your AWS credentials need s3:DeleteObject permission for bucket "{bucket_name}".'
                            }), 403
                        raise delete_err
        except ClientError as list_err:
            error_code = list_err.response.get('Error', {}).get('Code', '')
            if error_code == 'AccessDenied':
                return jsonify({
                    'success': False,
                    'error': f'Access Denied when listing objects. Your AWS credentials need s3:ListBucket permission for bucket "{bucket_name}".'
                }), 403
            raise list_err
        
        # Delete object versions if versioning is enabled
        try:
            version_paginator = s3.get_paginator('list_object_versions')
            for page in version_paginator.paginate(Bucket=bucket_name):
                versions = page.get('Versions', [])
                delete_markers = page.get('DeleteMarkers', [])
                
                to_delete = []
                for version in versions:
                    to_delete.append({'Key': version['Key'], 'VersionId': version['VersionId']})
                for marker in delete_markers:
                    to_delete.append({'Key': marker['Key'], 'VersionId': marker['VersionId']})
                
                if to_delete:
                    try:
                        s3.delete_objects(
                            Bucket=bucket_name,
                            Delete={'Objects': to_delete}
                        )
                        deleted_count += len(to_delete)
                        logger.info(f"[S3] Deleted {len(to_delete)} versions/markers from {bucket_name}")
                    except ClientError as version_err:
                        error_code = version_err.response.get('Error', {}).get('Code', '')
                        if error_code == 'AccessDenied':
                            logger.warning(f"[S3] Access Denied when deleting versions (may not have s3:DeleteObjectVersion permission)")
                        else:
                            logger.warning(f"[S3] Could not delete versions: {version_err}")
        except ClientError as version_list_err:
            error_code = version_list_err.response.get('Error', {}).get('Code', '')
            if error_code == 'AccessDenied':
                logger.warning(f"[S3] Access Denied when listing versions (versioning may not be enabled or missing s3:ListBucketVersions permission)")
            else:
                logger.warning(f"[S3] Could not list versions (versioning may not be enabled): {version_list_err}")
        except Exception as version_err:
            logger.warning(f"[S3] Error handling versions: {version_err}")

        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'message': f'S3 bucket {bucket_name} contents deleted successfully. Deleted {deleted_count} object(s).'
        })
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        
        if error_code == 'NoSuchBucket':
            return jsonify({
                'success': False,
                'error': f'S3 bucket {S3_BUCKET_NAME} does not exist.'
            }), 404
        elif error_code == 'AccessDenied' or 'Access Denied' in error_message:
            return jsonify({
                'success': False,
                'error': f'Access Denied: {error_message}\n\n'
                         f'Required IAM permissions for bucket "{S3_BUCKET_NAME}":\n'
                         f'- s3:ListBucket\n'
                         f'- s3:DeleteObject\n'
                         f'- s3:ListBucketVersions (if versioning enabled)\n'
                         f'- s3:DeleteObjectVersion (if versioning enabled)\n\n'
                         f'Attach "AmazonS3FullAccess" policy to your IAM user, or create a custom policy.'
            }), 403
        else:
            logger.error(f"Error deleting S3 contents: {e}")
            return jsonify({'success': False, 'error': f'AWS Error ({error_code}): {error_message}'}), 500
    except Exception as e:
        logger.error(f"Error deleting S3 contents: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/delete-ecr-repo', methods=['POST'])
@login_required
def delete_ecr_repo():
    """Delete ECR repository and all images across all AWS regions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # Get ECR repo name from request (preferred) or database (fallback)
        repo_name = data.get('repo_name', '').strip() or get_naming_config().get('ecr_repo_name', 'gbot-app-password-worker')
        logger.info(f"[ECR DELETE] Using repo name from REQUEST: {repo_name}")

        # List of all AWS regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]

        deleted_regions = []
        not_found_regions = []
        error_regions = []
        total_deleted = 0
        total_errors = 0

        def delete_region_ecr(target_region):
            """Delete ECR repository in a specific region (parallel execution)"""
            try:
                logger.info(f"[DELETE ECR] Processing region: {target_region}")
                session = get_boto3_session(access_key, secret_key, target_region)
                ecr = session.client("ecr")
    
                try:
                    ecr.delete_repository(
                        repositoryName=repo_name,
                        force=True
                    )
                    logger.info(f"[DELETE ECR] [{target_region}] ✓ Repository '{repo_name}' deleted successfully")
                    return {'success': True, 'region': target_region}
                except ecr.exceptions.RepositoryNotFoundException:
                    logger.info(f"[DELETE ECR] [{target_region}] Repository '{repo_name}' not found (skipping)")
                    return {'success': True, 'not_found': True, 'region': target_region}
                except Exception as e:
                    logger.error(f"[DELETE ECR] [{target_region}] ✗ Error: {e}")
                    return {'success': False, 'error': str(e), 'region': target_region}

            except Exception as e:
                logger.error(f"[DELETE ECR] [{target_region}] Error: {e}")
                return {'success': False, 'error': str(e), 'region': target_region}

        # Process all regions in parallel
        logger.info(f"[DELETE ECR] Starting parallel deletion across {len(AVAILABLE_GEO_REGIONS)} regions...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(delete_region_ecr, region): region for region in AVAILABLE_GEO_REGIONS}
            
            for future in as_completed(futures):
                region = futures[future]
                try:
                    result = future.result()
                    if result.get('success'):
                        if result.get('not_found'):
                            not_found_regions.append(region)
                        else:
                            deleted_regions.append(region)
                            total_deleted += 1
                    else:
                        error_regions.append(f"{region}: {result.get('error', 'Unknown error')}")
                        total_errors += 1
                except Exception as e:
                    logger.error(f"[DELETE ECR] [{region}] Future error: {e}")
                    total_errors += 1
                    error_regions.append(f"{region}: Future execution error - {str(e)}")

        return jsonify({
            'success': True,
            'deleted_regions': deleted_regions,
            'deleted_count': total_deleted,
            'not_found_regions': not_found_regions,
            'error_regions': error_regions if error_regions else None,
            'error_count': total_errors,
            'message': f'ECR repository cleanup completed across all regions. Deleted: {total_deleted}, Not found: {len(not_found_regions)}, Errors: {total_errors}'
        })
    except Exception as e:
        logger.error(f"Error deleting ECR repositories: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/delete-cloudwatch-logs', methods=['POST'])
@login_required
def delete_cloudwatch_logs():
    """Delete CloudWatch log groups for Lambdas across all AWS regions"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # Get log group prefix from request (preferred) or database (fallback)
        log_group_prefix = data.get('log_group_prefix', '').strip() or get_naming_config().get('lambda_prefix', 'gbot-chromium')
        # Convert lambda prefix to log group prefix format (e.g., "dev-chromium" -> "/aws/lambda/dev")
        if not log_group_prefix.startswith('/aws/lambda/'):
            prefix_part = log_group_prefix.split('-')[0] if '-' in log_group_prefix else log_group_prefix.split('_')[0] if '_' in log_group_prefix else log_group_prefix
            log_group_prefix = f"/aws/lambda/{prefix_part}"
        logger.info(f"[CLOUDWATCH DELETE] Using log group prefix from REQUEST: {log_group_prefix}")

        # List of all AWS regions (as specified by user)
        AVAILABLE_GEO_REGIONS = [
            # United States
            'us-east-1',      # N. Virginia
            'us-east-2',      # Ohio
            'us-west-1',      # N. California
            'us-west-2',      # Oregon
            # Africa
            'af-south-1',     # Cape Town
            # Asia Pacific
            'ap-east-1',      # Hong Kong
            'ap-east-2',      # Taipei
            'ap-south-1',     # Mumbai
            'ap-south-2',     # Hyderabad
            'ap-northeast-1', # Tokyo
            'ap-northeast-2', # Seoul
            'ap-northeast-3', # Osaka
            'ap-southeast-1', # Singapore
            'ap-southeast-2', # Sydney
            'ap-southeast-3', # Jakarta
            'ap-southeast-4', # Melbourne
            'ap-southeast-5', # Malaysia
            'ap-southeast-6', # New Zealand
            'ap-southeast-7', # Thailand
            # Canada
            'ca-central-1',   # Central
            'ca-west-1',      # Calgary
            # Europe
            'eu-central-1',   # Frankfurt
            'eu-west-1',      # Ireland
            'eu-west-2',      # London
            'eu-west-3',      # Paris
            'eu-north-1',     # Stockholm
            'eu-south-1',     # Milan
            'eu-south-2',     # Spain
            # Mexico
            'mx-central-1',   # Central
            # Middle East
            'me-south-1',     # Bahrain
            'me-central-1',   # UAE
            'il-central-1',   # Israel (Tel Aviv)
            # South America
            'sa-east-1',      # São Paulo
        ]

        all_deleted = []
        all_errors = []
        total_deleted = 0
        total_errors = 0

        def delete_region_cloudwatch(target_region):
            """Delete CloudWatch log groups in a specific region (parallel execution)"""
            region_deleted = []
            region_errors = []
            try:
                logger.info(f"[DELETE CLOUDWATCH] Processing region: {target_region}")
                session = get_boto3_session(access_key, secret_key, target_region)
                logs = session.client("logs")
    
                paginator = logs.get_paginator('describe_log_groups')
                for page in paginator.paginate():
                    for log_group in page.get('logGroups', []):
                        log_group_name = log_group['logGroupName']
                        if log_group_prefix in log_group_name:
                            try:
                                logs.delete_log_group(logGroupName=log_group_name)
                                region_deleted.append(f"{log_group_name} ({target_region})")
                            except Exception as e:
                                error_msg = f"{target_region}: {log_group_name} - {str(e)}"
                                logger.error(f"[DELETE CLOUDWATCH] [{target_region}] Error deleting {log_group_name}: {e}")
                                region_errors.append(error_msg)

                if region_deleted:
                    logger.info(f"[DELETE CLOUDWATCH] [{target_region}] Deleted {len(region_deleted)} log group(s)")

            except Exception as e:
                error_msg = f"{target_region}: Region processing failed - {str(e)}"
                logger.error(f"[DELETE CLOUDWATCH] [{target_region}] Error: {e}")
                region_errors.append(error_msg)
            
            return region_deleted, region_errors

        # Process all regions in parallel
        logger.info(f"[DELETE CLOUDWATCH] Starting parallel deletion across {len(AVAILABLE_GEO_REGIONS)} regions...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(delete_region_cloudwatch, region): region for region in AVAILABLE_GEO_REGIONS}
            
            for future in as_completed(futures):
                region = futures[future]
                try:
                    region_deleted, region_errors = future.result()
                    all_deleted.extend(region_deleted)
                    all_errors.extend(region_errors)
                    total_deleted += len(region_deleted)
                    total_errors += len(region_errors)
                except Exception as e:
                    logger.error(f"[DELETE CLOUDWATCH] [{region}] Future error: {e}")
                    total_errors += 1
                    all_errors.append(f"{region}: Future execution error - {str(e)}")

        return jsonify({
            'success': True,
            'deleted': all_deleted,
            'deleted_count': total_deleted,
            'error_count': total_errors,
            'errors': all_errors if all_errors else None,
            'message': f'CloudWatch log cleanup completed across all regions. Deleted: {total_deleted}, Errors: {total_errors}'
        })
    except Exception as e:
        logger.error(f"Error deleting CloudWatch logs: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/ec2-create-build-box', methods=['POST'])
@login_required
def ec2_create_build_box():
    """Create/Prepare EC2 Build Box"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        
        # Get custom naming from request (preferred) or database (fallback)
        instance_name = data.get('instance_name', 'gbot-ec2-build-box').strip() or 'gbot-ec2-build-box'
        ecr_repo_name = data.get('ecr_repo_name', 'gbot-app-password-worker').strip() or 'gbot-app-password-worker'
        s3_bucket_ec2 = data.get('s3_bucket', '').strip() or get_naming_config().get('s3_bucket', 'gbot-app-passwords')
        
        # Extract prefix from instance name (e.g., "dev" from "dev-ec2-build-box")
        prefix = instance_name.split('-')[0] if '-' in instance_name else 'gbot'
        
        logger.info(f"[EC2] Using instance name: {instance_name}")
        logger.info(f"[EC2] Using ECR repo: {ecr_repo_name}")
        logger.info(f"[EC2] Using S3 bucket: {s3_bucket_ec2}")
        logger.info(f"[EC2] Extracted prefix: {prefix}")

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        session = get_boto3_session(access_key, secret_key, region)
        account_id = get_account_id(session)

        # Ensure ECR repo exists with custom name
        if not create_ecr_repo(session, region, ecr_repo_name):
            return jsonify({'success': False, 'error': 'Failed to create or verify ECR repository'}), 500

        # Verify ECR repo
        ecr = session.client("ecr")
        try:
            resp = ecr.describe_repositories(repositoryNames=[ecr_repo_name])
            repo_uri = resp['repositories'][0]['repositoryUri']
            logger.info(f"[EC2] ✓ Verified ECR repo: {repo_uri}")
        except Exception as e:
            return jsonify({'success': False, 'error': f'ECR repository verification failed: {e}'}), 500
        
        # Ensure user's AWS credentials have S3 write permissions before uploading files
        logger.info("[EC2] Ensuring user has S3 write permissions...")
        s3_permissions_ensured = ensure_user_s3_permissions(session)
        if not s3_permissions_ensured:
            logger.warning("[EC2] Could not automatically attach S3 permissions. Will test S3 access before proceeding...")
        
        # Test S3 write access early (before creating EC2 resources) to fail fast
        logger.info(f"[EC2] Testing S3 write access for bucket {s3_bucket_ec2}...")
        s3_client = session.client("s3")
        try:
            # Ensure bucket exists first and get actual bucket name
            try:
                actual_bucket_name = create_s3_bucket(session, region, s3_bucket_ec2)
                if actual_bucket_name and actual_bucket_name != s3_bucket_ec2:
                    logger.info(f"[EC2] Bucket exists/created with name: {actual_bucket_name} (requested: {s3_bucket_ec2})")
                    s3_bucket_ec2 = actual_bucket_name  # Use the actual bucket name
            except Exception as bucket_err:
                logger.warning(f"[EC2] S3 bucket creation/verification warning: {bucket_err}")
            
            # Test write permission
            test_key = f"ec2-build-files/.test-write-permission-{int(time.time())}"
            s3_client.put_object(
                Bucket=s3_bucket_ec2,
                Key=test_key,
                Body=b"test",
                ContentType="text/plain"
            )
            # Clean up test object
            try:
                s3_client.delete_object(Bucket=s3_bucket_ec2, Key=test_key)
            except:
                pass
            logger.info(f"[EC2] ✓ S3 write permissions verified for bucket {s3_bucket_ec2}")
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'AccessDenied':
                error_msg = (
                    f"Access Denied to S3 bucket '{s3_bucket_ec2}'. "
                    f"Your AWS credentials do not have S3 write permissions.\n\n"
                    f"To fix this:\n"
                    f"1. Go to AWS IAM Console → Users → [Your IAM User]\n"
                    f"2. Click 'Add permissions' → 'Attach policies directly'\n"
                    f"3. Search for and attach 'AmazonS3FullAccess' policy\n"
                    f"4. Or create a custom policy with these permissions for bucket '{s3_bucket_ec2}':\n"
                    f"   - s3:PutObject\n"
                    f"   - s3:GetObject\n"
                    f"   - s3:DeleteObject\n"
                    f"   - s3:ListBucket\n\n"
                    f"After attaching the policy, wait 1-2 minutes for permissions to propagate, then try again."
                )
                logger.error(f"[EC2] {error_msg}")
                return jsonify({'success': False, 'error': error_msg}), 403
            else:
                return jsonify({'success': False, 'error': f"Failed to verify S3 write permissions: {e}"}), 500
        except Exception as e:
            logger.error(f"[EC2] Failed to test S3 permissions: {e}")
            return jsonify({'success': False, 'error': f"Failed to verify S3 access: {e}"}), 500
        
        # Create EC2 resources with custom names based on prefix
        role_arn = ensure_ec2_role_profile(session, prefix)
        sg_id = ensure_ec2_security_group(session, prefix)
        ensure_ec2_key_pair(session, prefix)

        create_ec2_build_box(session, account_id, region, role_arn, sg_id, instance_name, ecr_repo_name, s3_bucket_ec2)

        return jsonify({
            'success': True,
            'instance_name': instance_name,
            'ecr_repo_name': ecr_repo_name,
            'prefix': prefix,
            'message': f'EC2 build box "{instance_name}" launch requested. Wait ~5–10 minutes for Docker build & ECR push to complete.'
        })
    except Exception as e:
        logger.error(f"Error creating EC2 build box: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/get-aws-status', methods=['POST'])
@login_required
def get_aws_status():
    """Get comprehensive AWS status: Lambdas, ECR repos/images, DynamoDB for all geos"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # List of all AWS regions
        AVAILABLE_GEO_REGIONS = [
            'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
            'af-south-1',
            'ap-east-1', 'ap-east-2', 'ap-south-1', 'ap-south-2',
            'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
            'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3', 'ap-southeast-4',
            'ap-southeast-5', 'ap-southeast-6', 'ap-southeast-7',
            'ca-central-1', 'ca-west-1',
            'eu-central-1', 'eu-west-1', 'eu-west-2', 'eu-west-3',
            'eu-north-1', 'eu-south-1', 'eu-south-2',
            'mx-central-1',
            'me-south-1', 'me-central-1', 'il-central-1',
            'sa-east-1',
        ]

        status_data = {}

        def get_region_status(target_region):
            """Get status for a single region"""
            region_status = {
                'region': target_region,
                'lambdas': [],
                'lambda_count': 0,
                'ecr_repo_exists': False,
                'ecr_image_count': 0,
                'ecr_images': [],
                'error': None
            }
            
            try:
                session = get_boto3_session(access_key, secret_key, target_region)
                
                # Get Lambda functions
                try:
                    lam = session.client("lambda")
                    paginator = lam.get_paginator("list_functions")
                    for page in paginator.paginate():
                        for fn in page.get("Functions", []):
                            fn_name = fn["FunctionName"]
                            if PRODUCTION_LAMBDA_NAME in fn_name or "edu-gw" in fn_name:
                                region_status['lambdas'].append({
                                    'name': fn_name,
                                    'runtime': fn.get('Runtime', 'N/A'),
                                    'state': fn.get('State', 'Active'),
                                    'last_modified': fn.get('LastModified', 'N/A')
                                })
                    region_status['lambda_count'] = len(region_status['lambdas'])
                except Exception as e:
                    region_status['error'] = f"Lambda error: {str(e)}"
                
                # Get ECR repository and images
                try:
                    ecr = session.client("ecr")
                    try:
                        repo_response = ecr.describe_repositories(repositoryNames=[ECR_REPO_NAME])
                        if repo_response.get('repositories'):
                            region_status['ecr_repo_exists'] = True
                            # Get images
                            try:
                                images_response = ecr.list_images(repositoryName=ECR_REPO_NAME)
                                images = images_response.get('imageIds', [])
                                region_status['ecr_image_count'] = len(images)
                                region_status['ecr_images'] = [
                                    {
                                        'tag': img.get('imageTag', 'untagged'),
                                        'digest': img.get('imageDigest', 'N/A')[:20] + '...' if img.get('imageDigest') else 'N/A'
                                    }
                                    for img in images[:10]  # Limit to first 10
                                ]
                            except Exception as img_err:
                                region_status['error'] = f"ECR images error: {str(img_err)}"
                    except ClientError as repo_err:
                        if repo_err.response['Error']['Code'] != 'RepositoryNotFoundException':
                            region_status['error'] = f"ECR repo error: {str(repo_err)}"
                except Exception as e:
                    if not region_status['error']:
                        region_status['error'] = f"ECR error: {str(e)}"
                        
            except Exception as e:
                region_status['error'] = f"Region error: {str(e)}"
            
            return region_status

        # Get DynamoDB status (centralized in eu-west-1)
        dynamodb_status = {
            'region': 'eu-west-1',
            'table_exists': False,
            'item_count': 0,
            'error': None
        }
        try:
            # Use configurable DynamoDB table name
            dynamodb_table_status = data.get('dynamodb_table', '').strip() or get_naming_config().get('dynamodb_table', DEFAULT_DYNAMODB_TABLE)
            dynamodb_session = get_boto3_session(access_key, secret_key, 'eu-west-1')
            dynamodb = dynamodb_session.resource('dynamodb')
            table = dynamodb.Table(dynamodb_table_status)
            logger.info(f"[STATUS] Checking DynamoDB table: {dynamodb_table_status}")
            try:
                table.load()
                dynamodb_status['table_exists'] = True
                dynamodb_status['table_name'] = dynamodb_table_status
                # Get item count (approximate)
                try:
                    response = table.scan(Select='COUNT')
                    dynamodb_status['item_count'] = response.get('Count', 0)
                    # If count is large, get ScannedCount too
                    if 'ScannedCount' in response:
                        dynamodb_status['scanned_count'] = response['ScannedCount']
                except Exception as count_err:
                    dynamodb_status['error'] = f"Count error: {str(count_err)}"
            except ClientError as table_err:
                if table_err.response['Error']['Code'] != 'ResourceNotFoundException':
                    dynamodb_status['error'] = f"Table error: {str(table_err)}"
        except Exception as e:
            dynamodb_status['error'] = f"DynamoDB error: {str(e)}"

        # Get status for all regions in parallel
        logger.info(f"[STATUS] Getting AWS status for {len(AVAILABLE_GEO_REGIONS)} regions...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(get_region_status, region): region for region in AVAILABLE_GEO_REGIONS}
            
            for future in as_completed(futures):
                region = futures[future]
                try:
                    region_status = future.result()
                    status_data[region] = region_status
                except Exception as e:
                    logger.error(f"[STATUS] [{region}] Error: {e}")
                    status_data[region] = {
                        'region': region,
                        'lambdas': [],
                        'lambda_count': 0,
                        'ecr_repo_exists': False,
                        'ecr_image_count': 0,
                        'ecr_images': [],
                        'error': str(e)
                    }

        return jsonify({
            'success': True,
            'regions': status_data,
            'dynamodb': dynamodb_status,
            'total_regions': len(AVAILABLE_GEO_REGIONS)
        })
    except Exception as e:
        logger.error(f"Error getting AWS status: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# [DUPLICATE REMOVED] - empty_dynamodb_table is defined earlier in this file at line ~970

@aws_manager.route('/api/aws/delete-dynamodb-table', methods=['POST'])
@login_required
def delete_dynamodb_table():
    """Delete DynamoDB table (not just empty it)"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        # DynamoDB is centralized in eu-west-1
        dynamodb_region = 'eu-west-1'
        # Get table name from request (preferred) or database (fallback)
        table_name = data.get('table_name', '').strip() or get_naming_config().get('dynamodb_table', 'gbot-app-passwords')
        logger.info(f"[DYNAMODB DELETE] Using table name from REQUEST: {table_name}")
        
        session = get_boto3_session(access_key, secret_key, dynamodb_region)
        dynamodb = session.client('dynamodb')
        
        # [DEBUG] Check Account ID
        sts = session.client('sts')
        account_id = sts.get_caller_identity()['Account']
        logger.info(f"[DYNAMODB DELETE] Operating on Account ID: {account_id}, Region: {dynamodb_region}")
        
        try:
            dynamodb.delete_table(TableName=table_name)
            logger.info(f"[DYNAMODB] ✓ Deleted table '{table_name}' in {dynamodb_region}")
            return jsonify({
                'success': True,
                'message': f'DynamoDB table {table_name} deletion initiated in {dynamodb_region}.'
            })
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'ResourceNotFoundException':
                return jsonify({
                    'success': False,
                    'error': f'DynamoDB table {table_name} does not exist in {dynamodb_region} (Account: {account_id}).'
                }), 404
            raise e
    except Exception as e:
        logger.error(f"Error deleting DynamoDB table: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/clean-logs', methods=['POST'])
@login_required
def clean_logs():
    """Clean log output (simple endpoint for frontend)"""
    return jsonify({'success': True, 'message': 'Logs cleared'})

@aws_manager.route('/api/aws/ec2-show-status', methods=['POST'])
@login_required
def ec2_show_status():
    """Show EC2 Build Box Status"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        instance_name = data.get('instance_name', 'gbot-ec2-build-box').strip() or 'gbot-ec2-build-box'

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        logger.info(f"[EC2] Showing status for instance: {instance_name}")
        session = get_boto3_session(access_key, secret_key, region)
        inst = find_ec2_build_instance(session, instance_name)

        if not inst:
            return jsonify({
                'success': False,
                'error': 'No EC2 build box found.'
            }), 404

        state = inst["State"]["Name"]
        iid = inst["InstanceId"]
        pubip = inst.get("PublicIpAddress", "N/A")

        status_msg = f"Instance: {iid}\nState: {state}\nPublic IP: {pubip}\n\n"
        console_output = ""
        build_status = ""

        try:
            ec2 = session.client("ec2")
            console_output_resp = ec2.get_console_output(InstanceId=iid)
            console_output = console_output_resp.get('Output', '')
            
            if console_output:
                if "ECR_PUSH_DONE" in console_output or "EC2 Build Box User Data Script Completed Successfully" in console_output:
                    build_status = "✅ BUILD COMPLETED SUCCESSFULLY!\n\n"
                elif "FATAL:" in console_output or "ERROR:" in console_output:
                    build_status = "❌ BUILD FAILED - Check logs below\n\n"
                elif state == "running":
                    build_status = "⏳ BUILD IN PROGRESS...\n\n"
                
                lines = console_output.split('\n')
                recent_lines = lines[-50:] if len(lines) > 50 else lines
                status_msg += build_status
                status_msg += "Recent Console Output (last 50 lines):\n"
                status_msg += "=" * 60 + "\n"
                status_msg += '\n'.join(recent_lines)
        except Exception as console_err:
            status_msg += f"Could not retrieve console output: {console_err}\n"

        return jsonify({
            'success': True,
            'instance_id': iid,
            'state': state,
            'public_ip': pubip,
            'build_status': build_status,
            'console_output': console_output,
            'status_message': status_msg
        })
    except Exception as e:
        logger.error(f"Error checking EC2 status: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@aws_manager.route('/api/aws/ec2-terminate', methods=['POST'])
@login_required
def ec2_terminate():
    """Terminate EC2 Build Box"""
    try:
        data = request.get_json()
        access_key = data.get('access_key', '').strip()
        secret_key = data.get('secret_key', '').strip()
        region = data.get('region', '').strip()
        instance_name = data.get('instance_name', 'gbot-ec2-build-box').strip() or 'gbot-ec2-build-box'

        if not access_key or not secret_key or not region:
            return jsonify({'success': False, 'error': 'Please provide AWS credentials.'}), 400

        logger.info(f"[EC2] Terminating instance: {instance_name}")
        session = get_boto3_session(access_key, secret_key, region)
        inst = find_ec2_build_instance(session, instance_name)

        if not inst:
            return jsonify({
                'success': False,
                'error': 'No EC2 build box to terminate.'
            }), 404

        iid = inst["InstanceId"]
        ec2 = session.client("ec2")
        ec2.terminate_instances(InstanceIds=[iid])

        return jsonify({
            'success': True,
            'instance_id': iid,
            'message': f'Terminate requested for EC2 build box: {iid}'
        })
    except Exception as e:
        logger.error(f"Error terminating EC2 instance: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# Helper functions (adapted from aws.py)

def create_iam_role(session, role_name, service_principal, policy_arns):
    iam = session.client("iam")
    
    assume_role_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service_principal},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        resp = iam.get_role(RoleName=role_name)
        role_arn = resp["Role"]["Arn"]
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(assume_role_doc),
        )
    except iam.exceptions.NoSuchEntityException:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_doc),
            Description=f"Education case study role for {role_name}",
        )
        role_arn = resp["Role"]["Arn"]

    for p in policy_arns:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=p)
        except Exception as e:
            logger.warning(f"Could not attach policy {p} to {role_name}: {e}")

    time.sleep(10)  # Wait for propagation
    return role_arn

def ensure_lambda_role(session, role_name=None):
    """Ensure Lambda IAM role exists with custom name"""
    if role_name is None:
        naming_config = get_naming_config()
        role_name = naming_config.get('lambda_role_name', 'gbot-app-password-lambda-role')
    
    logger.info(f"[LAMBDA] Ensuring IAM role: {role_name}")
    iam = session.client("iam")
    lambda_policies = [
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",  # For app password storage
    ]
    
    try:
        resp = iam.get_role(RoleName=role_name)
        role_arn = resp["Role"]["Arn"]
        logger.info(f"[LAMBDA] ✓ IAM role '{role_name}' already exists")
        
        attached_policies = iam.list_attached_role_policies(RoleName=role_name)
        attached_policy_arns = [p['PolicyArn'] for p in attached_policies['AttachedPolicies']]
        
        for policy_arn in lambda_policies:
            if policy_arn not in attached_policy_arns:
                iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
                time.sleep(2)
        
        return role_arn
    except iam.exceptions.NoSuchEntityException:
        logger.info(f"[LAMBDA] Creating IAM role: {role_name}")
        return create_iam_role(
            session,
            role_name=role_name,
            service_principal="lambda.amazonaws.com",
            policy_arns=lambda_policies,
        )

def create_ecr_repo(session, region, repo_name=None):
    """Create ECR repository with configurable name for multi-tenant support"""
    if repo_name is None:
        naming_config = get_naming_config()
        repo_name = naming_config['ecr_repo_name']
    
    logger.info(f"[ECR] Creating/verifying repository: {repo_name}")
    ecr = session.client("ecr")
    try:
        resp = ecr.describe_repositories(repositoryNames=[repo_name])
        logger.info(f"[ECR] ✓ Repository '{repo_name}' already exists")
        return True
    except ecr.exceptions.RepositoryNotFoundException:
        try:
            ecr.create_repository(
                repositoryName=repo_name,
                imageTagMutability='MUTABLE',
                imageScanningConfiguration={'scanOnPush': False}
            )
            logger.info(f"[ECR] ✓ Created repository '{repo_name}'")
            time.sleep(2)
            return True
        except Exception as e:
            logger.error(f"[ECR] ✗ Error creating ECR repository '{repo_name}': {e}")
            raise

def create_s3_bucket(session, region, bucket_name=None):
    """Create S3 bucket with configurable name. Returns the actual bucket name used."""
    if bucket_name is None:
        naming_config = get_naming_config()
        bucket_name = naming_config['s3_bucket']
    
    original_bucket_name = bucket_name
    logger.info(f"[S3] Creating/verifying bucket: {bucket_name}")
    s3 = session.client("s3")
    account_id = session.client('sts').get_caller_identity()['Account']
    bucket_name_with_suffix = f"{bucket_name}-{account_id}"
    
    # First, try to check if bucket exists with original name
    bucket_exists_original = False
    try:
        s3.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        logger.info(f"[S3] ✓ Bucket '{bucket_name}' already exists")
        bucket_exists_original = True
    except ClientError as list_err:
        list_error_code = list_err.response.get('Error', {}).get('Code', '')
        if list_error_code == 'NoSuchBucket':
            # Bucket doesn't exist with original name, check if it exists with suffix
            logger.info(f"[S3] Bucket '{bucket_name}' doesn't exist, checking if '{bucket_name_with_suffix}' exists...")
            try:
                s3.list_objects_v2(Bucket=bucket_name_with_suffix, MaxKeys=1)
                logger.info(f"[S3] ✓ Bucket '{bucket_name_with_suffix}' already exists (with account ID suffix)")
                return bucket_name_with_suffix  # Return the actual bucket name
            except ClientError as suffix_err:
                suffix_error_code = suffix_err.response.get('Error', {}).get('Code', '')
                if suffix_error_code == 'NoSuchBucket':
                    # Neither bucket exists, will try to create with original name first
                    logger.info(f"[S3] Neither bucket exists, will try to create '{bucket_name}'")
                elif suffix_error_code in ['403', 'AccessDenied']:
                    # Access denied to suffix version too - will try to create with suffix
                    logger.info(f"[S3] Access denied to '{bucket_name_with_suffix}', will try to create with suffix")
                    bucket_name = bucket_name_with_suffix
                else:
                    # Some other error with suffix check
                    logger.warning(f"[S3] Error checking suffix bucket: {suffix_err}")
        elif list_error_code in ['403', 'AccessDenied']:
            # Access denied to original name - check if bucket exists with suffix
            logger.info(f"[S3] Access denied to '{bucket_name}', checking if bucket exists with account ID suffix: {bucket_name_with_suffix}")
            try:
                s3.list_objects_v2(Bucket=bucket_name_with_suffix, MaxKeys=1)
                logger.info(f"[S3] ✓ Bucket '{bucket_name_with_suffix}' already exists (with account ID suffix)")
                return bucket_name_with_suffix  # Return the actual bucket name
            except ClientError as suffix_err:
                suffix_error_code = suffix_err.response.get('Error', {}).get('Code', '')
                if suffix_error_code == 'NoSuchBucket':
                    # Bucket with suffix doesn't exist, will create with suffix
                    bucket_name = bucket_name_with_suffix
                    logger.info(f"[S3] Will create bucket with account ID suffix: {bucket_name}")
                else:
                    # Access denied to suffix version too - will try to create with suffix anyway
                    bucket_name = bucket_name_with_suffix
                    logger.info(f"[S3] Access denied to suffix version too, will try to create: {bucket_name}")
        else:
            raise list_err
    
    # If bucket exists with original name, return it
    if bucket_exists_original:
        return bucket_name
    
    try:
        if region == 'us-east-1':
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={'LocationConstraint': region}
            )
        logger.info(f"[S3] ✓ Created bucket '{bucket_name}'")
        
        try:
            s3.put_bucket_versioning(
                Bucket=bucket_name,
                VersioningConfiguration={'Status': 'Enabled'}
            )
        except:
            pass
        
        try:
            s3.put_public_access_block(
                Bucket=bucket_name,
                PublicAccessBlockConfiguration={
                    'BlockPublicAcls': True,
                    'IgnorePublicAcls': True,
                    'BlockPublicPolicy': True,
                    'RestrictPublicBuckets': True
                }
            )
        except:
            pass
        
        return bucket_name  # Return the actual bucket name that was created
    except ClientError as ce:
        raise

def inspect_iam(session):
    iam = session.client("iam")
    roles = []
    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page.get("Roles", []):
            if "edu-gw" in role["RoleName"]:
                roles.append({
                    'name': role['RoleName'],
                    'arn': role['Arn']
                })
    return roles

def inspect_ecr(session):
    ecr = session.client("ecr")
    repos = []
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repo in page.get("repositories", []):
            repos.append({
                'name': repo['repositoryName'],
                'uri': repo['repositoryUri']
            })
    return repos

def inspect_s3(session):
    s3 = session.client("s3")
    buckets = []
    try:
        resp = s3.list_buckets()
        for bucket in resp.get("Buckets", []):
            if "edu-gw" in bucket["Name"]:
                buckets.append({
                    'name': bucket['Name'],
                    'created': bucket.get('CreationDate', 'N/A').isoformat() if hasattr(bucket.get('CreationDate'), 'isoformat') else str(bucket.get('CreationDate', 'N/A'))
                })
    except Exception as e:
        logger.error(f"Error listing S3 buckets: {e}")
    return buckets

def inspect_lambdas(session):
    lam = session.client("lambda")
    lambdas = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            if "edu-gw" in fn["FunctionName"]:
                lambdas.append({
                    'name': fn['FunctionName'],
                    'runtime': fn.get('Runtime', 'N/A'),
                    'package_type': fn.get('PackageType', 'N/A')
                })
    return lambdas

def create_or_update_lambda(session, function_name, role_arn, timeout, env_vars, package_type, image_uri=None, code_str=None):
    lam = session.client("lambda")

    if package_type == "Image":
        if image_uri is None:
            raise ValueError("image_uri is required for Image package type")
        code_params = {"ImageUri": image_uri}
        runtime = None
        handler = None
    else:
        raise ValueError(f"Unsupported package type: {package_type}")

    try:
        lam.get_function(FunctionName=function_name)
        lam.update_function_code(FunctionName=function_name, **code_params, Publish=True)
        waiter = lam.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name, WaiterConfig={"Delay": 5, "MaxAttempts": 12})

        config_update_params = {
            "FunctionName": function_name,
            "Role": role_arn,
            "Timeout": timeout,
            "MemorySize": 2048,
            "Environment": {"Variables": env_vars},
            "EphemeralStorage": {"Size": 2048}
        }
        
        # Log environment variables being set (mask API key for security)
        env_vars_log = {k: (v[:10] + '...' if 'KEY' in k or 'SECRET' in k else v) for k, v in env_vars.items()}
        logger.info(f"[LAMBDA] Updating Lambda '{function_name}' configuration with environment variables: {env_vars_log}")
        
        lam.update_function_configuration(**config_update_params)
        
        # Verify environment variables were set
        try:
            updated_func = lam.get_function_configuration(FunctionName=function_name)
            updated_env = updated_func.get('Environment', {}).get('Variables', {})
            logger.info(f"[LAMBDA] ✓ Lambda '{function_name}' configuration updated. Environment variables set: {list(updated_env.keys())}")
            if 'TWOCAPTCHA_ENABLED' in updated_env:
                logger.info(f"[LAMBDA] ✓ TWOCAPTCHA_ENABLED = {updated_env.get('TWOCAPTCHA_ENABLED')}")
                if updated_env.get('TWOCAPTCHA_ENABLED') == 'true':
                    logger.info(f"[LAMBDA] ✓✓✓ 2Captcha is ENABLED in Lambda '{function_name}' environment!")
                else:
                    logger.warning(f"[LAMBDA] ⚠️ TWOCAPTCHA_ENABLED is set to 'false' in Lambda '{function_name}'")
            else:
                logger.error(f"[LAMBDA] ✗✗✗ TWOCAPTCHA_ENABLED NOT FOUND in Lambda '{function_name}' environment variables!")
        except Exception as verify_err:
            logger.warning(f"[LAMBDA] Could not verify environment variables for '{function_name}': {verify_err}")
        
        # CRITICAL: Aggressively remove reserved concurrency limit
        # Try multiple times to ensure it's removed (sometimes AWS API is eventually consistent)
        logger.info(f"[LAMBDA] Aggressively removing any reserved concurrency limits...")
        for attempt in range(3):
            try:
                concurrency_config = lam.get_function_concurrency(FunctionName=function_name)
                reserved_concurrency = concurrency_config.get('ReservedConcurrentExecutions')
                if reserved_concurrency:
                    logger.warning(f"[LAMBDA] Attempt {attempt + 1}: Found reserved concurrency = {reserved_concurrency}, deleting...")
                    lam.delete_function_concurrency(FunctionName=function_name)
                    time.sleep(2)  # Wait for propagation
                    logger.info(f"[LAMBDA] ✓ Deleted reserved concurrency limit")
                else:
                    logger.info(f"[LAMBDA] ✓ No reserved concurrency limit (good!)")
                    break
            except lam.exceptions.ResourceNotFoundException:
                logger.info(f"[LAMBDA] ✓ No reserved concurrency limit found (good!)")
                break
            except Exception as e:
                logger.warning(f"[LAMBDA] Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    # Final attempt: try to delete anyway
                    try:
                        lam.delete_function_concurrency(FunctionName=function_name)
                        logger.info(f"[LAMBDA] ✓ Force-deleted reserved concurrency limit")
                    except:
                        logger.error(f"[LAMBDA] Could not remove concurrency limit after 3 attempts")
    except lam.exceptions.ResourceNotFoundException:
        create_params = {
            "FunctionName": function_name,
            "Role": role_arn,
            "Code": code_params,
            "Timeout": timeout,
            "MemorySize": 2048,
            "Publish": True,
            "PackageType": package_type,
            "Environment": {"Variables": env_vars},
            "EphemeralStorage": {"Size": 2048}
        }
        try:
            lam.create_function(**create_params)
        except ClientError as create_err:
            error_code = create_err.response.get('Error', {}).get('Code', '')
            error_msg = create_err.response.get('Error', {}).get('Message', str(create_err))
            
            # Check for ECR image validation errors
            if error_code == 'InvalidParameterValueException' and ('is not valid' in error_msg or 'Source image' in error_msg):
                logger.error(f"[LAMBDA] ✗✗✗ CRITICAL: ECR image not found in region for {function_name}")
                logger.error(f"[LAMBDA] Error: {error_msg}")
                logger.error(f"[LAMBDA] Image URI: {image_uri}")
                logger.error(f"[LAMBDA] Lambda cannot pull ECR images from other regions.")
                logger.error(f"[LAMBDA] Solution: Push the ECR image to this region first using the 'Push ECR to All Regions' button.")
                raise ValueError(f"ECR image not found in region. Lambda cannot pull images cross-region. Error: {error_msg}")
            else:
                # Re-raise other errors
                raise
        
        # CRITICAL: Wait for function to be Active before modifying concurrency
        try:
            logger.info(f"[LAMBDA] Waiting for {function_name} to be active...")
            waiter = lam.get_waiter("function_active")
            waiter.wait(FunctionName=function_name, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
            logger.info(f"[LAMBDA] {function_name} is now active.")
        except Exception as e:
            logger.warning(f"[LAMBDA] Warning waiting for active state: {e}")

        # CRITICAL: Aggressively ensure no reserved concurrency limit for new functions
        logger.info(f"[LAMBDA] Aggressively ensuring no reserved concurrency limits on new function...")
        for attempt in range(3):
            try:
                concurrency_config = lam.get_function_concurrency(FunctionName=function_name)
                reserved_concurrency = concurrency_config.get('ReservedConcurrentExecutions')
                if reserved_concurrency:
                    logger.warning(f"[LAMBDA] Attempt {attempt + 1}: New function has reserved concurrency {reserved_concurrency}, deleting...")
                    lam.delete_function_concurrency(FunctionName=function_name)
                    time.sleep(2)  # Wait for propagation
                    logger.info(f"[LAMBDA] ✓ Deleted reserved concurrency limit for new function")
                else:
                    logger.info(f"[LAMBDA] ✓ New function created without reserved concurrency limit - using account limit (1000+)")
                    break
            except lam.exceptions.ResourceNotFoundException:
                logger.info(f"[LAMBDA] ✓ New function created without reserved concurrency limit - using account limit (1000+)")
                break
            except Exception as e:
                logger.warning(f"[LAMBDA] Attempt {attempt + 1} failed: {e}")
                if attempt < 2:
                    time.sleep(2)
                else:
                    # Final attempt: try to delete anyway
                    try:
                        lam.delete_function_concurrency(FunctionName=function_name)
                        logger.info(f"[LAMBDA] ✓ Force-deleted reserved concurrency limit for new function")
                    except:
                        logger.error(f"[LAMBDA] Could not remove concurrency limit after 3 attempts")

def ensure_ec2_role_profile(session, prefix='gbot'):
    """Create EC2 role and instance profile with custom prefix"""
    iam = session.client("iam")
    ec2_role_name = f"{prefix}-ec2-build-role"
    ec2_instance_profile_name = f"{prefix}-ec2-build-instance-profile"
    
    logger.info(f"[EC2] Creating IAM role: {ec2_role_name}")
    ec2_policies = [
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
        "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
    ]

    role_arn = create_iam_role(
        session,
        role_name=ec2_role_name,
        service_principal="ec2.amazonaws.com",
        policy_arns=ec2_policies,
    )

    try:
        iam.get_instance_profile(InstanceProfileName=ec2_instance_profile_name)
        logger.info(f"[EC2] ✓ Instance profile '{ec2_instance_profile_name}' already exists")
    except iam.exceptions.NoSuchEntityException:
        iam.create_instance_profile(InstanceProfileName=ec2_instance_profile_name)
        logger.info(f"[EC2] ✓ Created instance profile '{ec2_instance_profile_name}'")

    try:
        iam.add_role_to_instance_profile(
            InstanceProfileName=ec2_instance_profile_name,
            RoleName=ec2_role_name,
        )
    except iam.exceptions.LimitExceededException:
        pass

    time.sleep(10)
    return role_arn

def ensure_ec2_security_group(session, prefix='gbot'):
    """Create EC2 security group with custom prefix"""
    ec2 = session.client("ec2")
    ec2_security_group_name = f"{prefix}-ec2-build-sg"
    
    logger.info(f"[EC2] Creating security group: {ec2_security_group_name}")
    vpcs = ec2.describe_vpcs()
    default_vpc_id = vpcs["Vpcs"][0]["VpcId"]

    try:
        resp = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [ec2_security_group_name]},
                {"Name": "vpc-id", "Values": [default_vpc_id]},
            ]
        )
        if resp["SecurityGroups"]:
            logger.info(f"[EC2] ✓ Security group '{ec2_security_group_name}' already exists")
            return resp["SecurityGroups"][0]["GroupId"]
    except:
        pass

    resp = ec2.create_security_group(
        GroupName=ec2_security_group_name,
        Description=f"EC2 build box security group for {prefix} docker-selenium-lambda",
        VpcId=default_vpc_id,
    )
    sg_id = resp["GroupId"]
    logger.info(f"[EC2] ✓ Created security group '{ec2_security_group_name}': {sg_id}")

    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH from anywhere (demo)"}],
            }
        ],
    )

    return sg_id

def ensure_ec2_key_pair(session, prefix='gbot'):
    """Create EC2 key pair with custom prefix"""
    ec2 = session.client("ec2")
    ec2_key_pair_name = f"{prefix}-ec2-build-key"
    
    logger.info(f"[EC2] Creating key pair: {ec2_key_pair_name}")
    try:
        ec2.describe_key_pairs(KeyNames=[ec2_key_pair_name])
        logger.info(f"[EC2] ✓ Key pair '{ec2_key_pair_name}' already exists")
    except ClientError:
        resp = ec2.create_key_pair(KeyName=ec2_key_pair_name)
        private_key = resp["KeyMaterial"]
        key_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            f"{ec2_key_pair_name}.pem"
        )
        with open(key_path, "w", encoding="utf-8") as f:
            f.write(private_key)
        os.chmod(key_path, 0o400)
        logger.info(f"[EC2] ✓ Created key pair '{ec2_key_pair_name}'")

def create_ec2_build_box(session, account_id, region, role_arn, sg_id, instance_name='gbot-ec2-build-box', ecr_repo_name='gbot-app-password-worker', s3_bucket_name=None):
    """Create EC2 build box with custom naming"""
    if s3_bucket_name is None:
        naming_config = get_naming_config()
        s3_bucket_name = naming_config['s3_bucket']
    
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    s3 = session.client("s3")

    logger.info(f"[EC2] Creating EC2 instance: {instance_name}")
    logger.info(f"[EC2] Using ECR repo: {ecr_repo_name}")
    logger.info(f"[EC2] Using S3 bucket: {s3_bucket_name}")

    # Ensure S3 bucket exists before uploading (permissions already verified in route)
    logger.info(f"[EC2] Ensuring S3 bucket {s3_bucket_name} exists...")
    try:
        actual_bucket_name = create_s3_bucket(session, region, s3_bucket_name)
        if actual_bucket_name and actual_bucket_name != s3_bucket_name:
            logger.info(f"[EC2] Bucket exists/created with name: {actual_bucket_name} (requested: {s3_bucket_name})")
            s3_bucket_name = actual_bucket_name  # Use the actual bucket name
    except Exception as e:
        logger.warning(f"[EC2] S3 bucket creation warning: {e}")

    # Upload custom files to S3 for EC2 to download (permissions already verified)
    s3_build_prefix = "ec2-build-files"
    repo_files_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repo_aws_files")
    main_py_path = os.path.join(repo_files_dir, "main.py")
    dockerfile_path = os.path.join(repo_files_dir, "Dockerfile")
    
    if not os.path.exists(main_py_path):
        raise Exception(f"Custom main.py not found at {main_py_path}. Please ensure repo_aws_files/main.py exists.")
    
    # Upload main.py
    logger.info(f"[EC2] Found main.py at local path: {main_py_path}")
    logger.info(f"[EC2] Uploading custom main.py to S3: s3://{s3_bucket_name}/{s3_build_prefix}/main.py")
    
    try:
        with open(main_py_path, 'rb') as f:
            s3.put_object(
                Bucket=s3_bucket_name,
                Key=f"{s3_build_prefix}/main.py",
                Body=f.read(),
                ContentType="text/x-python"
            )
        logger.info(f"[EC2] Custom main.py uploaded successfully")
        
        # Upload Dockerfile if it exists
        if os.path.exists(dockerfile_path):
            logger.info(f"[EC2] Uploading custom Dockerfile to S3: s3://{s3_bucket_name}/{s3_build_prefix}/Dockerfile")
            with open(dockerfile_path, 'rb') as f:
                s3.put_object(
                    Bucket=s3_bucket_name,
                    Key=f"{s3_build_prefix}/Dockerfile",
                    Body=f.read(),
                    ContentType="text/plain"
                )
            logger.info(f"[EC2] Custom Dockerfile uploaded successfully")
        else:
            logger.warning(f"[EC2] Dockerfile not found at {dockerfile_path}, will use default from repo")
            
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == 'AccessDenied':
            raise Exception(f"Access Denied to S3 bucket {s3_bucket_name}. Please ensure your AWS credentials have S3 write permissions (s3:PutObject).")
        else:
            raise Exception(f"Failed to upload files to S3: {e}")
    except Exception as e:
        logger.error(f"[EC2] Failed to upload files to S3: {e}")
        raise Exception(f"Failed to upload files to S3: {e}")

    param = ssm.get_parameter(
        Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"
    )
    ami_id = param["Parameter"]["Value"]

    instance_type = "c5.xlarge"  # Using c5.xlarge for high network bandwidth (10Gbps)

    repo_uri_base = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ecr_repo_name}"

    # User data script that downloads custom main.py from S3
    # Using .format() instead of f-string to avoid issues with bash array syntax [@]
    user_data = """#!/bin/bash
set -xe
exec > >(tee /var/log/user-data.log) 2>&1
echo "=== EC2 Build Box User Data Script Started ==="
date

amazon-linux-extras install docker -y || yum install -y docker
systemctl enable docker
systemctl start docker
usermod -a -G docker ec2-user
yum install -y git unzip

curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
./aws/install

cd /home/ec2-user
echo "Cloning docker-selenium-lambda repo..."
git clone https://github.com/umihico/docker-selenium-lambda.git
cd docker-selenium-lambda

echo "Downloading custom files from S3..."
aws s3 cp s3://{s3_bucket}/{s3_prefix}/main.py ./main.py
if [ $? -eq 0 ]; then
    echo "Custom main.py downloaded successfully"
    chmod 644 main.py
else
    echo "WARNING: Failed to download custom main.py, using default from repo"
fi

aws s3 cp s3://{s3_bucket}/{s3_prefix}/Dockerfile ./Dockerfile
if [ $? -eq 0 ]; then
    echo "Custom Dockerfile downloaded successfully"
    chmod 644 Dockerfile
else
    echo "INFO: No custom Dockerfile found, using default from repo"
fi

echo "Verifying ECR repository exists..."
ECR_FOUND=0
for i in {{1..60}}; do
    if aws ecr describe-repositories --repository-names {ecr_repo} --region {region} 2>/dev/null; then
        echo "ECR repository found!"
        ECR_FOUND=1
        break
    fi
    echo "Waiting for ECR repository... ($i/60)"
    sleep 1
done

if [ $ECR_FOUND -eq 0 ]; then
    echo "WARNING: ECR repository {ecr_repo} not found after 60 seconds!"
    if aws ecr create-repository --repository-name {ecr_repo} --region {region} --image-tag-mutability MUTABLE 2>/dev/null; then
        echo "ECR repository created successfully!"
        sleep 3
        ECR_FOUND=1
    fi
fi

if [ $ECR_FOUND -eq 0 ]; then
    echo "FATAL: ECR repository verification failed. Exiting."
    exit 1
fi

echo "Logging into ECR..."
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com

echo "Building Docker image..."
docker build -t {ecr_repo}:{image_tag} .

echo "Tagging Docker image..."
docker tag {ecr_repo}:{image_tag} {repo_uri}:{image_tag}

echo "Pushing Docker image to ECR..."
docker push {repo_uri}:{image_tag}

echo "Verifying image push..."
aws ecr describe-images --repository-name {ecr_repo} --image-ids imageTag={image_tag} --region {region}

touch /home/ec2-user/ECR_PUSH_DONE
echo "=== ECR Push to Source Region Completed ==="

# Now push image to all other AWS regions for multi-region Lambda support
echo ""
echo "=========================================="
echo "Starting Multi-Region ECR Push"
echo "=========================================="

SOURCE_ECR_URI="{repo_uri}:{image_tag}"
ACCOUNT_ID="{account_id}"
REPO_NAME="{ecr_repo}"
IMAGE_TAG="{image_tag}"
SOURCE_REGION="{region}"

# List of all AWS regions (as specified by user - 34 regions total)
TARGET_REGIONS=(
    "us-east-1" "us-east-2" "us-west-1" "us-west-2"
    "af-south-1"
    "ap-east-1" "ap-east-2" "ap-south-1" "ap-south-2"
    "ap-northeast-1" "ap-northeast-2" "ap-northeast-3"
    "ap-southeast-1" "ap-southeast-2" "ap-southeast-3"
    "ap-southeast-4" "ap-southeast-5" "ap-southeast-6" "ap-southeast-7"
    "ca-central-1" "ca-west-1"
    "eu-central-1" "eu-west-1" "eu-west-2" "eu-west-3"
    "eu-north-1" "eu-south-1" "eu-south-2"
    "mx-central-1"
    "me-south-1" "me-central-1" "il-central-1"
    "sa-east-1"
)

SUCCESS_COUNT=0
FAILED_COUNT=0
FAILED_REGIONS=()

# Function to push to a single region (for parallel execution)
push_region() {{
    TARGET_REGION=$1
    TARGET_ECR_URI="$ACCOUNT_ID.dkr.ecr.$TARGET_REGION.amazonaws.com/$REPO_NAME:$IMAGE_TAG"
    
    echo "[$TARGET_REGION] Starting push process..."
    
    # Check if image already exists
    if aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$IMAGE_TAG" --region "$TARGET_REGION" 2>/dev/null; then
        echo "[$TARGET_REGION] ✓ Image already exists, skipping..."
        echo "[$TARGET_REGION] SUCCESS" > /tmp/ecr_push_$TARGET_REGION.result
        return 0
    fi
    
    # Create ECR repository if it doesn't exist
    if ! aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$TARGET_REGION" 2>/dev/null; then
        echo "[$TARGET_REGION] Creating ECR repository..."
        aws ecr create-repository --repository-name "$REPO_NAME" --region "$TARGET_REGION" --image-tag-mutability MUTABLE 2>/dev/null || true
        sleep 1  # Reduced wait time
    fi
    
    # Authenticate with target region
    if ! aws ecr get-login-password --region "$TARGET_REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$TARGET_REGION.amazonaws.com" 2>/dev/null; then
        echo "[$TARGET_REGION] ✗ Failed to authenticate"
        echo "[$TARGET_REGION] FAILED" > /tmp/ecr_push_$TARGET_REGION.result
        return 1
    fi
    
    # Tag image for target region
    docker tag "$SOURCE_ECR_URI" "$TARGET_ECR_URI" 2>/dev/null || {{
        echo "[$TARGET_REGION] ✗ Failed to tag image"
        echo "[$TARGET_REGION] FAILED" > /tmp/ecr_push_$TARGET_REGION.result
        return 1
    }}
    
    # Push image to target region
    if docker push "$TARGET_ECR_URI" 2>/dev/null; then
        # Verify image exists after push (optimized)
        sleep 2  # Reduced wait time
        VERIFIED=0
        for verify_attempt in {{1..3}}; do
            if aws ecr describe-images --repository-name "$REPO_NAME" --image-ids imageTag="$IMAGE_TAG" --region "$TARGET_REGION" 2>&1; then
                echo "[$TARGET_REGION] ✓✓✓ VERIFIED: Image exists in ECR!"
                VERIFIED=1
                break
            fi
            sleep 2  # Reduced wait time
        done
        
        if [ $VERIFIED -eq 1 ]; then
            echo "[$TARGET_REGION] ✓ Successfully pushed and verified"
            echo "[$TARGET_REGION] SUCCESS" > /tmp/ecr_push_$TARGET_REGION.result
            return 0
        else
            echo "[$TARGET_REGION] ✗ Push completed but verification failed"
            echo "[$TARGET_REGION] FAILED" > /tmp/ecr_push_$TARGET_REGION.result
            return 1
        fi
    else
        echo "[$TARGET_REGION] ✗ Failed to push"
        echo "[$TARGET_REGION] FAILED" > /tmp/ecr_push_$TARGET_REGION.result
        return 1
    fi
}}

# Export function for parallel execution
export -f push_region
export SOURCE_ECR_URI ACCOUNT_ID REPO_NAME IMAGE_TAG SOURCE_REGION

# Process regions in PARALLEL using GNU parallel or background jobs
echo "Starting PARALLEL push to all regions..."
echo "Using up to 20 concurrent pushes for faster completion"

# Use background jobs for parallel execution
PIDS=()
for TARGET_REGION in "${{" + "TARGET_REGIONS[@]" + "}}"; do
    # Skip source region
    if [ "$TARGET_REGION" = "$SOURCE_REGION" ]; then
        continue
    fi
    
    # Run push in background (limit to 20 concurrent)
    while [ $(jobs -r | wc -l) -ge 20 ]; do
        sleep 1
    done
    
    push_region "$TARGET_REGION" &
    PIDS+=($!)
done

# Wait for all background jobs to complete
echo "Waiting for all pushes to complete..."
for PID in "${{PIDS[@]}}"; do
    wait $PID
done

# Collect results
for TARGET_REGION in "${{" + "TARGET_REGIONS[@]" + "}}"; do
    if [ "$TARGET_REGION" = "$SOURCE_REGION" ]; then
        continue
    fi
    
    if [ -f "/tmp/ecr_push_$TARGET_REGION.result" ]; then
        if grep -q "SUCCESS" "/tmp/ecr_push_$TARGET_REGION.result"; then
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            FAILED_COUNT=$((FAILED_COUNT + 1))
            FAILED_REGIONS+=("$TARGET_REGION")
        fi
        rm -f "/tmp/ecr_push_$TARGET_REGION.result"
    fi
done

echo ""
echo "=========================================="
echo "Multi-Region Push Summary"
echo "=========================================="
echo "Success: $SUCCESS_COUNT regions"
echo "Failed: $FAILED_COUNT regions"
if [ $FAILED_COUNT -gt 0 ]; then
    echo "Failed regions: ${{FAILED_REGIONS[*]}}"
fi
echo "=========================================="

touch /home/ec2-user/MULTI_REGION_PUSH_DONE
echo "=== EC2 Build Box User Data Script Completed Successfully ==="
date
""".format(
        s3_bucket=s3_bucket_name,
        s3_prefix=s3_build_prefix,
        ecr_repo=ecr_repo_name,
        region=region,
        account_id=account_id,
        image_tag=ECR_IMAGE_TAG,
        repo_uri=repo_uri_base
    )

    # Extract prefix for resource names
    prefix = instance_name.split('-')[0] if '-' in instance_name else 'gbot'
    ec2_instance_profile_name = f"{prefix}-ec2-build-instance-profile"
    ec2_key_pair_name = f"{prefix}-ec2-build-key"
    
    logger.info(f"[EC2] Creating instance with Name tag: {instance_name}")
    logger.info(f"[EC2] Using instance profile: {ec2_instance_profile_name}")
    logger.info(f"[EC2] Using key pair: {ec2_key_pair_name}")

    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": ec2_instance_profile_name},
        SecurityGroupIds=[sg_id],
        KeyName=ec2_key_pair_name,
        UserData=user_data,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": instance_name},
                    {"Key": "Purpose", "Value": f"{prefix}-docker-selenium-lambda-build"},
                ],
            }
        ],
    )
    instance_id = resp["Instances"][0]["InstanceId"]
    logger.info(f"[EC2] ✓ Created EC2 instance: {instance_id} with name: {instance_name}")
    return instance_id

def find_ec2_build_instance(session, instance_name=None):
    """Find EC2 build instance by name tag - tries exact match first, then pattern match"""
    if instance_name is None:
        naming_config = get_naming_config()
        # CORRECTED: ec2_instance_name is the full name like 'default-ec2-build-box'
        # constructed from instance_name (prefix) + '-ec2-build-box' at line 91
        instance_name = naming_config.get('ec2_instance_name', 'default-ec2-build-box')
        logger.info(f"[EC2] Using EC2 instance name from config: {instance_name}")
    
    logger.info(f"[EC2] Searching for instance with Name tag: {instance_name}")
    ec2 = session.client("ec2")
    
    # First, try exact match
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [instance_name]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    for r in resp.get("Reservations", []):
        for inst in r.get("Instances", []):
            logger.info(f"[EC2] ✓ Found instance with exact name match: {instance_name}")
            return inst
    
    # If not found, try to find instances with "ec2-build-box" pattern (for backward compatibility)
    logger.info(f"[EC2] Exact match not found, trying pattern search for instances with 'ec2-build-box' in name...")
    resp = ec2.describe_instances(
        Filters=[
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    
    # Look for instances with Purpose tag containing "docker-selenium-lambda-build" or Name containing "ec2-build-box"
    for r in resp.get("Reservations", []):
        for inst in r.get("Instances", []):
            tags = {tag['Key']: tag['Value'] for tag in inst.get('Tags', [])}
            name_tag = tags.get('Name', '')
            purpose_tag = tags.get('Purpose', '')
            
            # Check if it matches our pattern
            if 'ec2-build-box' in name_tag.lower() or 'docker-selenium-lambda-build' in purpose_tag.lower():
                logger.info(f"[EC2] ✓ Found instance with pattern match: Name={name_tag}, Purpose={purpose_tag}")
                return inst
    
    logger.warning(f"[EC2] ✗ No EC2 build instance found with name '{instance_name}' or matching pattern")
    return None
